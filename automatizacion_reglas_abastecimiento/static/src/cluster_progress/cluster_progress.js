/** @odoo-module **/

/**
 * Avance en vivo de la actualización por Cluster: una barra por fase.
 *
 * Mismo esquema que el avance de la conciliación de stock
 * (`integracion_wis/static/src/conciliacion_progress`) y el de la importación
 * masiva: copiado y no reutilizado porque este módulo no depende de ninguno de
 * los dos. Polling liviano cada 4 s sólo mientras el proceso corre.
 */

import { Component, onWillUnmount, useEffect, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { _t } from "@web/core/l10n/translation";

const MODELO = "cluster.proceso";
const INTERVALO_POLL = 4000;
const INTERVALO_RELOJ = 1000;
const CAMPOS = [
    "fase", "estado", "etapa", "error_msg", "con_rutas",
    "rutas_total", "rutas_hecho", "rutas_cambiadas", "rutas_started_at", "rutas_ended_at",
    "reglas_total", "reglas_hecho", "reglas_creadas", "reglas_modificadas", "reglas_borradas",
    "reglas_sin_cambio", "reglas_started_at", "reglas_ended_at", "write_date",
];
const CAMPOS_FECHA = CAMPOS.filter((c) => c.endsWith("_at") || c === "write_date");
const ALFA = 0.3;

const TERMINADO = { texto: _t("Terminado"), clase: "text-success" };
const CON_ERROR = { texto: _t("Detenido"), clase: "text-danger" };
const CANCELADO = { texto: _t("Cancelado"), clase: "text-muted" };

function estaVivo(d) {
    return d.estado === "en_proceso";
}

function estadoFase(d, enFase, pasada) {
    if (pasada) {
        return TERMINADO;
    }
    if (enFase && d.estado === "detenido") {
        return CON_ERROR;
    }
    if (enFase && d.estado === "cancelado") {
        return CANCELADO;
    }
    return null;
}

function faseRutas(d) {
    const enFase = d.fase === "rutas";
    return {
        clave: "rutas",
        titulo: _t("Fase 1 · Rutas de los productos"),
        corriendo: estaVivo(d) && (enFase || d.fase === "cola"),
        cargando: d.fase === "cola",
        estadoFinal: estadoFase(d, enFase, ["reglas", "terminado"].includes(d.fase)),
        hecho: d.rutas_hecho,
        total: d.rutas_total,
        inicio: d.rutas_started_at,
        fin: d.rutas_ended_at,
        unidad: _t("productos/s"),
        etapa: d.etapa,
        textoEtapa: _t("Una sola escritura de rutas por producto. Podés cerrar esta pantalla."),
        errorMsg: enFase && d.estado === "detenido" ? d.error_msg : null,
        filasContadores: [[
            { etiqueta: _t("Productos"), valor: d.rutas_hecho },
            { etiqueta: _t("Con rutas cambiadas"), valor: d.rutas_cambiadas, clase: "text-success" },
        ]],
    };
}

function faseReglas(d) {
    const enFase = d.fase === "reglas";
    return {
        clave: "reglas",
        titulo: d.con_rutas
            ? _t("Fase 2 · Reglas de abastecimiento de las variantes")
            : _t("Reglas de abastecimiento de las variantes"),
        corriendo: estaVivo(d) && enFase,
        cargando: false,
        estadoFinal: estadoFase(d, enFase, d.fase === "terminado"),
        hecho: d.reglas_hecho,
        total: d.reglas_total,
        inicio: d.reglas_started_at,
        fin: d.reglas_ended_at,
        unidad: _t("variantes/s"),
        etapa: d.etapa,
        textoEtapa: _t("Sólo se crea, modifica o borra lo que cambia. Podés cerrar esta pantalla."),
        errorMsg: enFase && d.estado === "detenido" ? d.error_msg : null,
        filasContadores: [[
            { etiqueta: _t("Reglas creadas"), valor: d.reglas_creadas, clase: "text-success" },
            { etiqueta: _t("Modificadas"), valor: d.reglas_modificadas },
            { etiqueta: _t("Borradas"), valor: d.reglas_borradas },
            { etiqueta: _t("Sin cambio"), valor: d.reglas_sin_cambio, clase: "text-muted" },
        ]],
    };
}

export function fasesCluster(d) {
    const fases = [];
    if (d.con_rutas) {
        fases.push(faseRutas(d));
    }
    if (!d.con_rutas || ["reglas", "terminado"].includes(d.fase)) {
        fases.push(faseReglas(d));
    }
    return fases;
}

export class ClusterProcesoProgress extends Component {
    static template = "automatizacion_reglas_abastecimiento.ClusterProcesoProgress";
    static props = { ...standardFieldProps };

    setup() {
        this.orm = useService("orm");
        this.state = useState({ vivo: null, ahora: Date.now(), ritmos: {} });
        this.timerPoll = null;
        this.timerReloj = null;
        this.ultimasMuestras = {};
        this.yaRecargo = false;
        this.idActual = null;
        this.leyoInicial = false;
        this.ultimaFirma = null;

        // Se miran los valores del registro Y los de la última lectura: si sólo
        // se mirara el polling, un cambio hecho por un botón (que recarga el
        // registro) no se vería nunca.
        useEffect(
            () => this._sincronizarTimers(),
            () => [
                this._firma(this.props.record.data),
                this.state.vivo && this._firma(this.state.vivo),
                this.props.record.resId,
            ]
        );
        onWillUnmount(() => this._pararTodo());
    }

    _firma(d) {
        return `${d.fase}|${d.estado}`;
    }

    _pararTodo() {
        if (this.timerPoll) {
            clearInterval(this.timerPoll);
            this.timerPoll = null;
        }
        if (this.timerReloj) {
            clearInterval(this.timerReloj);
            this.timerReloj = null;
        }
    }

    _sincronizarTimers() {
        const id = this.props.record.resId;
        if (id !== this.idActual) {
            this.idActual = id;
            this.leyoInicial = false;
            this.ultimasMuestras = {};
            this.yaRecargo = false;
            this.ultimaFirma = null;
            this.state.vivo = null;
        }
        // El registro cambió (lo recargó un botón): es más nuevo que el polling.
        const firma = this._firma(this.props.record.data);
        if (firma !== this.ultimaFirma) {
            this.ultimaFirma = firma;
            if (this.state.vivo && this._firma(this.state.vivo) !== firma) {
                this.state.vivo = null;
                this.ultimasMuestras = {};
                this.leyoInicial = false;
            }
        }
        // Una lectura inicial aunque el registro diga que no corre: cubre abrir
        // la pantalla sobre una consulta que arrancó el cron hace un rato.
        if (id && !this.leyoInicial) {
            this.leyoInicial = true;
            this._consultar();
        }
        if (!estaVivo(this.datos)) {
            this._pararTodo();
            this.ultimasMuestras = {};
            return;
        }
        if (this.timerPoll) {
            return;
        }
        this.yaRecargo = false;
        this.timerPoll = setInterval(() => this._consultar(), INTERVALO_POLL);
        this.timerReloj = setInterval(() => {
            this.state.ahora = Date.now();
        }, INTERVALO_RELOJ);
        this._consultar();
    }

    async _consultar() {
        const id = this.props.record.resId;
        if (!id) {
            return;
        }
        let filas;
        try {
            filas = await this.orm.read(MODELO, [id], CAMPOS);
        } catch {
            return;   // se reintenta en el próximo tick
        }
        if (!filas || !filas.length) {
            return;
        }
        const datos = filas[0];
        this._actualizarRitmos(datos);
        this.state.vivo = datos;
        if (!estaVivo(datos)) {
            // Terminó o se detuvo: se recarga el formulario una vez, para que
            // los botones, el log y la pestaña de requests queden al día.
            this._pararTodo();
            if (!this.yaRecargo && this.timerPollHabiaArrancado) {
                this.yaRecargo = true;
                this.props.record.load();
            }
        } else {
            this.timerPollHabiaArrancado = true;
        }
    }

    /** Ritmo medido de salto a salto (el avance llega con el commit de cada tanda). */
    _actualizarRitmos(datos) {
        const ahora = Date.now();
        for (const fase of fasesCluster(Object.assign({}, this._base(), datos))) {
            if (!fase.corriendo || fase.cargando) {
                continue;
            }
            const previa = this.ultimasMuestras[fase.clave];
            if (!previa) {
                this.ultimasMuestras[fase.clave] = { hecho: fase.hecho, t: ahora };
                continue;
            }
            const dt = (ahora - previa.t) / 1000;
            const dn = fase.hecho - previa.hecho;
            if (dn === 0) {
                continue;
            }
            this.ultimasMuestras[fase.clave] = { hecho: fase.hecho, t: ahora };
            if (dt <= 0 || dn < 0) {
                continue;
            }
            const instantaneo = dn / dt;
            const anterior = this.state.ritmos[fase.clave];
            this.state.ritmos[fase.clave] = anterior
                ? ALFA * instantaneo + (1 - ALFA) * anterior
                : instantaneo;
        }
    }

    _base() {
        const d = this.props.record.data;
        const base = {};
        for (const campo of CAMPOS) {
            const v = d[campo];
            base[campo] = CAMPOS_FECHA.includes(campo) || typeof v === "string" ? v : v || 0;
        }
        return base;
    }

    get datos() {
        const base = this._base();
        return this.state.vivo ? Object.assign({}, base, this.state.vivo) : base;
    }

    get secciones() {
        return fasesCluster(this.datos).map((fase) => this._decorar(fase));
    }

    _decorar(fase) {
        const hayErrores = fase.filasContadores.flat().some((c) => c.error && (c.valor || 0) > 0)
            || Boolean(fase.errorMsg);
        const segundos = this._segundos(fase);
        return Object.assign({}, fase, {
            porcentaje: this._porcentaje(fase),
            detalleBarra: fase.cargando ? "" : `${this.fmt(fase.hecho)} / ${this.fmt(fase.total)}`,
            hayErrores,
            transcurrido: segundos === null ? "—" : this._hhmmss(segundos),
            eta: this._eta(fase),
            ritmoTexto: this._ritmoTexto(fase),
            filasContadores: fase.filasContadores.map((fila) =>
                fila.map((c) => Object.assign({}, c, {
                    texto: this.fmt(c.valor),
                    claseValor: c.error
                        ? ((c.valor || 0) > 0 ? "fs-5 fw-bold text-danger" : "fs-5 fw-bold text-muted")
                        : `fs-5 fw-bold ${c.clase || ""}`,
                    claseCaja: c.error && (c.valor || 0) > 0
                        ? "border rounded p-2 text-center border-danger"
                        : "border rounded p-2 text-center",
                }))
            ),
            claseColumna: (fila) => (fila.length === 4 ? "col-6 col-md-3"
                : fila.length === 3 ? "col-6 col-md-4"
                : fila.length === 2 ? "col-6" : "col-12 col-md-6"),
        });
    }

    _porcentaje(fase) {
        if (fase.cargando) {
            return 0;
        }
        if (fase.estadoFinal === TERMINADO && !fase.total) {
            return 100;
        }
        return fase.total ? Math.min(100, Math.round((fase.hecho / fase.total) * 100)) : 0;
    }

    _segundos(fase) {
        const inicio = this._aMilis(fase.inicio);
        if (!inicio) {
            return null;
        }
        // Una fase detenida o cancelada no tiene fin: se mide hasta la última
        // vez que se escribió el registro, que es cuando se detuvo.
        const fin = this._aMilis(fase.fin)
            || (fase.corriendo ? this.state.ahora : this._aMilis(this.datos.write_date));
        if (!fin) {
            return null;
        }
        return Math.max(0, Math.round((fin - inicio) / 1000));
    }

    _eta(fase) {
        if (!fase.corriendo || fase.cargando) {
            return null;
        }
        const faltan = fase.total - fase.hecho;
        if (faltan <= 0) {
            return _t("terminando…");
        }
        const ritmo = this.state.ritmos[fase.clave];
        if (!ritmo || ritmo <= 0) {
            return _t("calculando…");
        }
        return this._hhmmss(Math.round(faltan / ritmo));
    }

    _ritmoTexto(fase) {
        const r = fase.cargando ? 0 : this.state.ritmos[fase.clave];
        if (!r) {
            return "";
        }
        const valor = r < 10 ? r.toFixed(1) : Math.round(r).toLocaleString();
        return `${valor} ${fase.unidad}`;
    }

    _aMilis(valor) {
        if (!valor) {
            return null;
        }
        if (typeof valor === "string") {
            return new Date(valor.replace(" ", "T") + "Z").getTime();
        }
        return valor.toMillis ? valor.toMillis() : null;
    }

    _hhmmss(total) {
        const h = Math.floor(total / 3600);
        const m = Math.floor((total % 3600) / 60);
        const s = total % 60;
        const dd = (n) => String(n).padStart(2, "0");
        return `${dd(h)}:${dd(m)}:${dd(s)}`;
    }

    fmt(n) {
        return (n || 0).toLocaleString();
    }
}

registry.category("fields").add("cluster_proceso_progress", {
    component: ClusterProcesoProgress,
    supportedTypes: ["integer"],
});
