/** @odoo-module **/

/**
 * Avance en vivo de la conciliación de stock: una barra por fase.
 *
 * Es el mismo esquema que el avance de la importación masiva
 * (`forum_partner_import/static/src/live_progress`), copiado y no reutilizado
 * porque este módulo es compartido entre clientes y no puede depender de uno de
 * Forum.
 *
 * POLLING Y NO BUS. Un read liviano cada 4 segundos, sólo mientras algo corre y
 * sólo con la pantalla abierta. Cuando nada corre no hay ningún timer.
 *
 * SE PINTA DESDE `props.record.data`. Lo que llega del polling se superpone
 * encima; sin polling se ve el registro tal cual.
 *
 * LAS FASES 3 A 5 SON DEL BATCH DE INVENTARIO. El registro las espeja en campos
 * calculados `cs_b_*`: acá sólo se dibujan.
 */

import { Component, onWillUnmount, useEffect, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { _t } from "@web/core/l10n/translation";

const MODELO = "conciliacion.stock";
const INTERVALO_POLL = 4000;
const INTERVALO_RELOJ = 1000;
// Estados del batch en los que su cron está trabajando.
const BATCH_VIVO = ["applying", "posting", "reconciling"];
const CAMPOS = [
    "fase", "estado", "cs_total", "cs_done", "cs_encontradas", "cs_sin_stock",
    "cs_no_existe", "cs_err_filas", "cs_errors", "cs_con_diferencia", "cs_step",
    "cs_started_at", "cs_ended_at", "cs_pagina", "cs_paginas_total",
    "cs_etapa_consulta", "cs_estrategia", "cs_pc_total", "cs_pc_done", "cs_pc_started_at",
    "cs_error_msg", "cs_tabla_started_at", "cs_tabla_ended_at",
    "cs_ajuste_started_at", "cs_ajuste_ended_at", "cs_ajuste_celdas",
    "cs_requests", "cs_requests_error", "cs_ultimo_request",
    "cs_batch_estado", "cs_b_fase",
    "cs_b_apply_total", "cs_b_apply_done", "cs_b_applied", "cs_b_apply_errors",
    "cs_b_apply_started_at", "cs_b_apply_ended_at",
    "cs_b_post_total", "cs_b_post_done", "cs_b_post_errors",
    "cs_b_post_started_at", "cs_b_post_ended_at",
    "cs_b_rec_total", "cs_b_rec_done", "cs_b_rec_errors", "cs_b_rec_lines",
    "cs_b_rec_started_at", "cs_b_rec_ended_at", "write_date",
];
const CAMPOS_FECHA = CAMPOS.filter((c) => c.endsWith("_at") || c === "write_date");
const ALFA = 0.3;

const TERMINADO = { texto: _t("Terminado"), clase: "text-success" };
const CON_ERROR = { texto: _t("Con error"), clase: "text-danger" };
const CANCELADO = { texto: _t("Cancelado"), clase: "text-muted" };

function consultaViva(d) {
    return d.fase === "consultando" && d.estado !== "error";
}

function estaVivo(d) {
    return consultaViva(d) || BATCH_VIVO.includes(d.cs_batch_estado);
}

/** Fase 0: la tabla de trabajo. Es sincrónica: cuando se ve, ya terminó. */
function faseTabla(d) {
    return {
        clave: "tabla",
        titulo: _t("Fase 0 · Armado de la tabla"),
        corriendo: false,
        estadoFinal: TERMINADO,
        hecho: d.cs_total,
        total: d.cs_total,
        inicio: d.cs_tabla_started_at,
        fin: d.cs_tabla_ended_at,
        unidad: _t("variantes/s"),
        filasContadores: [[
            { etiqueta: _t("Variantes integradas a consultar"), valor: d.cs_total },
        ]],
    };
}

function detenida(d) {
    return d.fase === "consultando" && d.estado === "error";
}

/** Fase 1, estrategia listado: páginas de `/ConsultaDeStock/GetData`. */
function faseListado(d) {
    const sondeo = d.cs_etapa_consulta === "sondeo" || !d.cs_etapa_consulta;
    const enListado = d.cs_etapa_consulta === "listado";
    let estadoFinal = null;
    if (!sondeo && !enListado) {
        estadoFinal = TERMINADO;
    } else if (detenida(d)) {
        estadoFinal = CON_ERROR;
    } else if (d.fase === "tabla") {
        estadoFinal = CANCELADO;
    }
    return {
        clave: "listado",
        titulo: _t("Fase 1 · Consulta a WIS: listado de stock"),
        corriendo: consultaViva(d) && (sondeo || enListado),
        cargando: sondeo,
        estadoFinal,
        hecho: d.cs_pagina,
        total: d.cs_paginas_total,
        inicio: d.cs_started_at,
        fin: d.cs_pc_started_at || d.cs_ended_at,
        unidad: _t("páginas/s"),
        etapa: d.cs_step,
        textoEtapa: sondeo
            ? _t("Midiendo cuántas páginas tiene el listado para poder mostrar cuánto falta.")
            : _t("WIS devuelve 10 productos por página. Podés cerrar esta pantalla."),
        errorMsg: detenida(d) && (sondeo || enListado) ? d.cs_error_msg : null,
        nota: d.cs_ultimo_request,
        filasContadores: [
            [
                { etiqueta: _t("Páginas leídas"), valor: d.cs_pagina },
                { etiqueta: _t("Variantes encontradas"), valor: d.cs_encontradas, clase: "text-success" },
                { etiqueta: _t("Con diferencia"), valor: d.cs_con_diferencia },
                { etiqueta: _t("Requests con error"), valor: d.cs_requests_error, error: true },
            ],
        ],
    };
}

/** Fase 1, por código: estrategia principal, o segunda pasada del listado. */
function fasePorCodigo(d) {
    const enCurso = d.cs_etapa_consulta === "por_codigo" && ["consultando", "tabla"].includes(d.fase);
    let estadoFinal = null;
    if (!enCurso) {
        estadoFinal = TERMINADO;
    } else if (detenida(d)) {
        estadoFinal = CON_ERROR;
    } else if (d.fase === "tabla") {
        estadoFinal = CANCELADO;
    }
    return {
        clave: "por_codigo",
        titulo: d.cs_estrategia === "listado"
            ? _t("Fase 1b · Consulta por código de lo que el listado no trajo")
            : _t("Fase 1 · Consulta a WIS por código"),
        corriendo: consultaViva(d) && d.cs_etapa_consulta === "por_codigo",
        cargando: false,
        estadoFinal,
        hecho: d.cs_pc_done,
        total: d.cs_pc_total,
        inicio: d.cs_pc_started_at,
        fin: d.cs_ended_at,
        unidad: _t("códigos/s"),
        etapa: d.cs_step,
        textoEtapa: _t("Un request por variante: WIS distingue «sin stock» de «no existe». Podés cerrar esta pantalla."),
        errorMsg: detenida(d) && d.cs_etapa_consulta === "por_codigo" ? d.cs_error_msg : null,
        nota: d.cs_ultimo_request,
        filasContadores: [
            [
                { etiqueta: _t("Con stock en WIS"), valor: d.cs_encontradas, clase: "text-success" },
                { etiqueta: _t("Sin stock en WIS (entran como 0)"), valor: d.cs_sin_stock },
                { etiqueta: _t("No existen en WIS"), valor: d.cs_no_existe, error: true },
                { etiqueta: _t("Con error"), valor: d.cs_err_filas, error: true },
            ],
            [
                { etiqueta: _t("Con diferencia"), valor: d.cs_con_diferencia },
                { etiqueta: _t("Requests a WIS"), valor: d.cs_requests },
                { etiqueta: _t("Fuera del ajuste"), valor: d.cs_errors, error: true },
            ],
        ],
    };
}

/** Fase 2: el ajuste. Sincrónico, como la tabla. */
function faseAjuste(d) {
    return {
        clave: "ajuste",
        titulo: _t("Fase 2 · Generación del ajuste"),
        corriendo: false,
        estadoFinal: TERMINADO,
        hecho: d.cs_ajuste_celdas,
        total: d.cs_ajuste_celdas,
        inicio: d.cs_ajuste_started_at,
        fin: d.cs_ajuste_ended_at,
        unidad: _t("celdas/s"),
        filasContadores: [[
            { etiqueta: _t("Celdas con diferencia"), valor: d.cs_ajuste_celdas },
        ]],
    };
}

/** Estado final de una fase del batch: terminada si el batch ya pasó por ella. */
function estadoBatch(d, fase, terminados) {
    if (terminados.includes(d.cs_batch_estado)) {
        return TERMINADO;
    }
    if (d.cs_b_fase === fase && d.cs_batch_estado === "error") {
        return CON_ERROR;
    }
    if (d.cs_b_fase === fase && d.cs_batch_estado === "cancel") {
        return CANCELADO;
    }
    return null;
}

function faseAplicar(d) {
    return {
        clave: "aplicar",
        titulo: _t("Fase 3 · Aplicación del ajuste"),
        corriendo: d.cs_batch_estado === "applying",
        estadoFinal: estadoBatch(d, "apply", ["applied", "posting", "posted", "reconciling", "reconciled"]),
        hecho: d.cs_b_apply_done,
        total: d.cs_b_apply_total,
        inicio: d.cs_b_apply_started_at,
        fin: d.cs_b_apply_ended_at,
        unidad: _t("celdas/s"),
        filasContadores: [[
            { etiqueta: _t("Quants ajustados"), valor: d.cs_b_applied, clase: "text-success" },
            { etiqueta: _t("Errores"), valor: d.cs_b_apply_errors, error: true },
        ]],
    };
}

function fasePublicar(d) {
    return {
        clave: "publicar",
        titulo: _t("Fase 4 · Publicación de los asientos"),
        corriendo: d.cs_batch_estado === "posting",
        estadoFinal: estadoBatch(d, "post", ["posted", "reconciling", "reconciled"]),
        hecho: d.cs_b_post_done,
        total: d.cs_b_post_total,
        inicio: d.cs_b_post_started_at,
        fin: d.cs_b_post_ended_at,
        unidad: _t("asientos/s"),
        filasContadores: [[
            { etiqueta: _t("Asientos publicados"), valor: d.cs_b_post_done, clase: "text-success" },
            { etiqueta: _t("Errores"), valor: d.cs_b_post_errors, error: true },
        ]],
    };
}

function faseConciliar(d) {
    return {
        clave: "conciliar",
        titulo: _t("Fase 5 · Conciliación de los asientos"),
        corriendo: d.cs_batch_estado === "reconciling",
        estadoFinal: estadoBatch(d, "rec", ["reconciled"]),
        hecho: d.cs_b_rec_done,
        total: d.cs_b_rec_total,
        inicio: d.cs_b_rec_started_at,
        fin: d.cs_b_rec_ended_at,
        unidad: _t("grupos/s"),
        filasContadores: [[
            { etiqueta: _t("Grupos conciliados"), valor: d.cs_b_rec_done, clase: "text-success" },
            { etiqueta: _t("Líneas"), valor: d.cs_b_rec_lines },
            { etiqueta: _t("Errores"), valor: d.cs_b_rec_errors, error: true },
        ]],
    };
}

/** Qué fases se dibujan, según hasta dónde llegó el proceso. */
export function fasesConciliacion(d) {
    const fases = [];
    if (d.fase === "borrador" || !d.cs_total) {
        return fases;
    }
    fases.push(faseTabla(d));
    const consultaArrancada = d.cs_started_at || d.cs_etapa_consulta;
    if (consultaArrancada) {
        const hayListado = d.cs_estrategia === "listado"
            || ["sondeo", "listado"].includes(d.cs_etapa_consulta) || !d.cs_etapa_consulta;
        if (hayListado && (d.cs_etapa_consulta || d.fase === "consultando")) {
            fases.push(faseListado(d));
        }
        if (d.cs_etapa_consulta === "por_codigo" || d.cs_pc_total) {
            fases.push(fasePorCodigo(d));
        }
    }
    if (d.cs_ajuste_ended_at) {
        fases.push(faseAjuste(d));
    }
    const b = d.cs_batch_estado;
    if (b && (d.cs_b_apply_started_at || ["applying", "applied", "posting", "posted", "reconciling", "reconciled"].includes(b))) {
        fases.push(faseAplicar(d));
    }
    if (b && (d.cs_b_post_started_at || ["posting", "posted", "reconciling", "reconciled"].includes(b))) {
        fases.push(fasePublicar(d));
    }
    if (b && (d.cs_b_rec_started_at || ["reconciling", "reconciled"].includes(b))) {
        fases.push(faseConciliar(d));
    }
    return fases;
}

export class WisConciliacionProgress extends Component {
    static template = "integracion_wis.ConciliacionProgress";
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
        return `${d.fase}|${d.estado}|${d.cs_batch_estado}`;
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
        for (const fase of fasesConciliacion(Object.assign({}, this._base(), datos))) {
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
        return fasesConciliacion(this.datos).map((fase) => this._decorar(fase));
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

registry.category("fields").add("wis_conciliacion_progress", {
    component: WisConciliacionProgress,
    supportedTypes: ["integer"],
});
