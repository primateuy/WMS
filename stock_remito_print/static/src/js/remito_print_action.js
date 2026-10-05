/** @odoo-module **/

import { Component, onMounted, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { browser } from "@web/core/browser/browser";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

// Si el navegador no avisa que terminó de imprimir (por ejemplo porque el
// usuario dejó el diálogo abierto), se sigue igual pasado este tiempo para
// no dejar el proceso colgado.
const PRINT_FALLBACK_TIMEOUT = 60000;

/**
 * Decodifica la cabecera X-Remito-Result, que viaja en base64 porque las
 * cabeceras HTTP son latin-1 y los mensajes van en español.
 */
function decodeBase64Utf8(valor) {
    const binario = atob(valor);
    const bytes = Uint8Array.from(binario, (caracter) => caracter.charCodeAt(0));
    return new TextDecoder("utf-8").decode(bytes);
}

export class RemitoPrintAction extends Component {
    static template = "stock_remito_print.BatchAction";
    static props = { "*": true };

    setup() {
        this.actionService = useService("action");
        const params = (this.props.action && this.props.action.params) || {};
        this.pickingIds = params.picking_ids || [];
        this.mode = params.mode || "print";
        this.markPrinted = Boolean(params.mark_printed);
        this.batchSize = params.batch_size || 20;
        this.today = new Date().toISOString().slice(0, 10);
        this.batches = this._makeBatches(this.pickingIds, this.batchSize);

        this.state = useState({
            running: false,
            finished: false,
            cancelled: false,
            cancelRequested: false,
            currentBatch: 0,
            totalBatches: this.batches.length,
            total: this.pickingIds.length,
            ok: 0,
            ucfe: 0,
            standard: 0,
            errors: 0,
            errorList: [],
            standardList: [],
        });

        onMounted(() => this.run());
    }

    // ------------------------------------------------------------------
    // Estado derivado
    // ------------------------------------------------------------------

    get progress() {
        if (!this.state.totalBatches) {
            return 0;
        }
        const hechos = this.state.finished
            ? this.state.totalBatches
            : Math.max(this.state.currentBatch - 1, 0);
        return Math.round((hechos / this.state.totalBatches) * 100);
    }

    get modeLabel() {
        return this.mode === "download" ? _t("Descargar") : _t("Imprimir");
    }

    // ------------------------------------------------------------------
    // Proceso por lotes
    // ------------------------------------------------------------------

    /**
     * Cede el control al navegador para que pinte lo último que se puso en
     * el estado. Hace falta antes de cualquier trabajo que bloquee el hilo
     * principal: window.print() lo bloquea, así que sin esto la pantalla se
     * queda mostrando el lote anterior y los contadores en cero, y parece
     * que el proceso está colgado.
     */
    _repintar() {
        return new Promise((resolve) => browser.setTimeout(resolve, 0));
    }

    _makeBatches(ids, tamano) {
        const lotes = [];
        for (let indice = 0; indice < ids.length; indice += tamano) {
            lotes.push(ids.slice(indice, indice + tamano));
        }
        return lotes;
    }

    async run() {
        this.state.running = true;
        for (let indice = 0; indice < this.batches.length; indice++) {
            if (this.state.cancelRequested) {
                this.state.cancelled = true;
                break;
            }
            this.state.currentBatch = indice + 1;
            await this._repintar();
            const lote = this.batches[indice];
            try {
                // Los lotes se procesan de a uno: hasta que no termina el
                // actual no se pide el siguiente.
                await this._processBatch(lote, indice + 1);
            } catch (error) {
                this.state.errors += lote.length;
                this.state.errorList.push({
                    id: 0,
                    name: _t("Lote %s", indice + 1),
                    error: (error && error.message) || String(error),
                });
            }
        }
        this.state.running = false;
        this.state.finished = true;
    }

    async _processBatch(ids, indice) {
        const formData = new FormData();
        formData.append("picking_ids", JSON.stringify(ids));
        formData.append("mark_printed", this.markPrinted ? "1" : "0");
        formData.append("batch_index", String(indice));
        formData.append("batch_date", this.today);
        if (odoo.csrf_token) {
            formData.append("csrf_token", odoo.csrf_token);
        }

        const response = await browser.fetch("/stock_remito_print/batch", {
            method: "POST",
            body: formData,
        });

        const contentType = response.headers.get("Content-Type") || "";
        if (contentType.includes("application/json")) {
            // El lote no produjo ningún PDF: solo hay errores que informar.
            const payload = await response.json();
            this._collectResults(payload.results || []);
            if (payload.error && !(payload.results || []).length) {
                throw new Error(payload.error);
            }
            return;
        }
        if (!response.ok) {
            throw new Error(
                _t("El servidor respondió con el código %s", response.status)
            );
        }

        const cabecera = response.headers.get("X-Remito-Result");
        if (cabecera) {
            try {
                const payload = JSON.parse(decodeBase64Utf8(cabecera));
                this._collectResults(payload.results || []);
            } catch {
                // Si la cabecera viene mal no se pierde el PDF del lote.
            }
        }

        const blob = await response.blob();
        // Los contadores ya están actualizados: que se vean antes de que
        // la impresión bloquee el hilo.
        await this._repintar();
        if (this.mode === "download") {
            this._download(blob, indice);
        } else {
            await this._print(blob);
        }
    }

    _collectResults(resultados) {
        for (const resultado of resultados) {
            if (resultado.source === "error") {
                this.state.errors++;
                this.state.errorList.push({
                    id: resultado.id,
                    name: resultado.name,
                    error: resultado.error,
                });
                continue;
            }
            this.state.ok++;
            if (resultado.source === "ucfe") {
                this.state.ucfe++;
                continue;
            }
            this.state.standard++;
            this.state.standardList.push({
                id: resultado.id,
                name: resultado.name,
                fallback: resultado.source === "standard_fallback",
            });
        }
    }

    // ------------------------------------------------------------------
    // Salida: impresión y descarga
    // ------------------------------------------------------------------

    _print(blob) {
        const url = URL.createObjectURL(blob);
        return new Promise((resolve) => {
            const iframe = document.createElement("iframe");
            iframe.style.display = "none";
            iframe.src = url;
            let terminado = false;

            const limpiar = () => {
                if (terminado) {
                    return;
                }
                terminado = true;
                browser.clearTimeout(temporizador);
                // Se le da un respiro al navegador antes de sacar el iframe:
                // si se quita enseguida algunos cancelan la impresión.
                browser.setTimeout(() => {
                    iframe.remove();
                    URL.revokeObjectURL(url);
                    resolve();
                }, 500);
            };

            const temporizador = browser.setTimeout(limpiar, PRINT_FALLBACK_TIMEOUT);

            iframe.onload = () => {
                try {
                    const ventana = iframe.contentWindow;
                    ventana.addEventListener("afterprint", limpiar, { once: true });
                    ventana.focus();
                    ventana.print();
                } catch {
                    limpiar();
                }
            };
            document.body.appendChild(iframe);
        });
    }

    _download(blob, indice) {
        const url = URL.createObjectURL(blob);
        const enlace = document.createElement("a");
        enlace.href = url;
        enlace.download = `Remitos_${this.today}_lote_${indice}.pdf`;
        document.body.appendChild(enlace);
        enlace.click();
        enlace.remove();
        browser.setTimeout(() => URL.revokeObjectURL(url), 1000);
    }

    // ------------------------------------------------------------------
    // Botones
    // ------------------------------------------------------------------

    onCancel() {
        // La cancelación se respeta entre lotes: el que está en curso
        // termina para no dejar operaciones marcadas a medias.
        this.state.cancelRequested = true;
    }

    onClose() {
        if (this.env.dialogData && this.env.dialogData.close) {
            this.env.dialogData.close();
        }
        // Se refresca la vista de atrás para que se vean las marcas nuevas.
        this.actionService.doAction({ type: "ir.actions.client", tag: "soft_reload" });
    }
}

registry.category("actions").add("stock_remito_print.batch", RemitoPrintAction);
