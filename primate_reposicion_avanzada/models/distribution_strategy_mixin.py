# -*- coding: utf-8 -*-
import logging
import math

from odoo import api, models
from odoo.tools import float_compare, float_round

_logger = logging.getLogger(__name__)

# Estrategias disponibles. Se expone como constante para que los consumidores externos
# puedan armar su propio campo Selection sin duplicar las etiquetas.
DISTRIBUTION_STRATEGIES = [
    ('proporcional', 'Proporcional a la demanda'),
    ('por_demanda', 'Por demanda (mayor primero)'),
    ('por_prioridad_almacen', 'Por prioridad de almacén'),
    ('igualitaria', 'Igualitaria'),
    ('por_rotacion', 'Por rotación de producto'),
]


class PrimateDistributionStrategyMixin(models.AbstractModel):
    _name = 'primate.distribution.strategy.mixin'
    _description = 'Motor reusable de distribución de stock insuficiente'

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    @api.model
    def compute_partial_distribution(self, strategy, candidates, available_qty, multiple,
                                     rounding_method='ceil', priority_ids=None,
                                     precision_rounding=0.01):
        """Reparte una cantidad disponible entre varios candidatos según una estrategia.

        Desacoplado de cualquier modelo concreto: trabaja sobre listas de diccionarios, así
        que sirve tanto para las líneas de un proceso de reposición como para reglas de
        reabastecimiento o cualquier otro conjunto de registros que compita por un stock.

        :param strategy: una de las claves de DISTRIBUTION_STRATEGIES.
        :param candidates: lista de dicts, cada uno con:
            - 'key': identificador del candidato (id de registro o cualquier hashable).
            - 'demand_qty': cantidad que pide. Nadie recibe más que esto.
            - 'ads' (opcional): venta diaria promedio, solo para 'por_rotacion'.
            - 'sequence' (opcional): orden de desempate; por defecto el orden de la lista.
        :param available_qty: cantidad total a repartir.
        :param multiple: múltiplo de distribución. 0 o 1 = sin múltiplo.
        :param rounding_method: 'ceil' o 'floor'. Solo afecta el ajuste final al múltiplo;
            el reparto nunca supera available_qty ni la demanda de cada candidato, así que
            en la práctica el ajuste es siempre hacia abajo dentro de esos topes.
        :param priority_ids: lista ordenada de 'key', solo para 'por_prioridad_almacen'.
            Los candidatos que no figuren se atienden al final.
        :param precision_rounding: paso mínimo cuando el producto no tiene múltiplo.
            Sin esto, un reparto fraccionario redondeado por la precisión decimal del
            campo destino puede sumar más que available_qty (33 candidatos a 39.8484…
            se guardan como 39.85 y totalizan 1315.05 sobre 1315 disponibles).
        :return: dict {key: cantidad asignada}. Las claves ausentes valen 0.
        """
        candidatos = self._normalize_candidates(candidates)
        asignado = {c['key']: 0.0 for c in candidatos}
        if not candidatos or available_qty <= 0:
            return asignado

        multiple = self._sane_multiple(multiple)
        # Paso de reparto: el múltiplo del producto, o la precisión decimal si no tiene.
        paso = multiple if multiple > 1 else (precision_rounding or 0.01)

        if strategy == 'igualitaria':
            return self._dist_equal(candidatos, available_qty, multiple, paso)
        if strategy == 'proporcional':
            return self._dist_proportional(candidatos, available_qty, multiple, paso)
        if strategy == 'por_demanda':
            ordenados = sorted(candidatos,
                               key=lambda c: (-c['demand_qty'], c['sequence']))
            return self._dist_sequential(ordenados, available_qty, multiple, paso)
        if strategy == 'por_prioridad_almacen':
            return self._dist_sequential(
                self._sort_by_priority(candidatos, priority_ids), available_qty, multiple, paso)
        if strategy == 'por_rotacion':
            return self._dist_sequential(
                self._sort_by_rotation(candidatos, priority_ids), available_qty, multiple, paso)

        _logger.warning("Estrategia de distribución desconocida: %r, se usa proporcional.",
                        strategy)
        return self._dist_proportional(candidatos, available_qty, multiple, paso)

    # ------------------------------------------------------------------
    # Utilitarios
    # ------------------------------------------------------------------

    @api.model
    def _normalize_candidates(self, candidates):
        """Valida y completa los candidatos, descartando los que no piden nada."""
        normalizados = []
        for indice, candidato in enumerate(candidates or []):
            demanda = float(candidato.get('demand_qty') or 0.0)
            if demanda <= 0:
                continue
            normalizados.append({
                'key': candidato['key'],
                'demand_qty': demanda,
                'ads': float(candidato.get('ads') or 0.0),
                'sequence': candidato.get('sequence', indice),
            })
        return normalizados

    @api.model
    def _sane_multiple(self, multiple):
        """Un múltiplo de 0 o negativo equivale a no tener múltiplo."""
        try:
            multiple = float(multiple or 1)
        except (TypeError, ValueError):
            multiple = 1.0
        return multiple if multiple > 1 else 1.0

    @api.model
    def _round_to_multiple(self, qty, multiple, max_qty=None, rounding_method='floor',
                           paso=None):
        """Ajusta al múltiplo (o al paso, si no hay múltiplo) sin pasar de max_qty."""
        if qty <= 0:
            return 0.0
        if multiple <= 1:
            if max_qty is not None:
                qty = min(qty, max_qty)
            if paso:
                # Hacia abajo: repartir de más es peor que repartir de menos, porque el
                # sobrante se reparte después en pasos exactos.
                qty = float_round(qty, precision_rounding=paso, rounding_method='DOWN')
            return qty
        ratio = float_round(qty / multiple, precision_digits=6)
        ajustada = (math.ceil(ratio) if rounding_method == 'ceil' else math.floor(ratio)) * multiple
        if max_qty is not None and float_compare(ajustada, max_qty, precision_rounding=0.0001) > 0:
            ajustada = math.floor(float_round(max_qty / multiple, precision_digits=6)) * multiple
        return float(max(ajustada, 0.0))

    # ------------------------------------------------------------------
    # Estrategias
    # ------------------------------------------------------------------

    @api.model
    def _dist_proportional(self, candidatos, disponible, multiple, paso=None):
        """Reparte en proporción a la demanda, en pasos de un múltiplo.

        El sobrante del redondeo se reparte de mayor a menor demanda, un múltiplo por vez,
        hasta agotar el disponible o hasta que ningún candidato admita un múltiplo más.
        """
        demanda_total = sum(c['demand_qty'] for c in candidatos)
        asignado = {c['key']: 0.0 for c in candidatos}
        if demanda_total <= 0:
            return asignado

        for candidato in candidatos:
            proporcion = (candidato['demand_qty'] / demanda_total) * disponible
            asignado[candidato['key']] = self._round_to_multiple(
                proporcion, multiple, max_qty=candidato['demand_qty'], paso=paso)

        paso = paso or multiple
        sobrante = disponible - sum(asignado.values())
        for candidato in sorted(candidatos, key=lambda c: (-c['demand_qty'], c['sequence'])):
            while sobrante >= paso and \
                    asignado[candidato['key']] + paso <= candidato['demand_qty'] + 1e-9:
                asignado[candidato['key']] += paso
                sobrante -= paso
        return asignado

    @api.model
    def _dist_sequential(self, candidatos_ordenados, disponible, multiple, paso=None):
        """Abastece completo en el orden dado hasta agotar el disponible.

        El primero que no se cubre entero recibe el remanente ajustado al múltiplo hacia
        abajo; los siguientes quedan en cero.
        """
        asignado = {c['key']: 0.0 for c in candidatos_ordenados}
        restante = disponible
        for candidato in candidatos_ordenados:
            if restante < (paso or multiple):
                continue
            tope = min(candidato['demand_qty'], restante)
            cantidad = self._round_to_multiple(tope, multiple, max_qty=tope, paso=paso)
            asignado[candidato['key']] = cantidad
            restante -= cantidad
        return asignado

    @api.model
    def _dist_equal(self, candidatos, disponible, multiple, paso=None):
        """Partes iguales, sin mirar cuánto pidió cada uno.

        Si la parte igual no alcanza para un múltiplo, se reparte de a un múltiplo por vez
        en rondas, por orden de secuencia, hasta agotar.
        """
        asignado = {c['key']: 0.0 for c in candidatos}
        if not candidatos:
            return asignado

        por_candidato = disponible / len(candidatos)
        base = self._round_to_multiple(por_candidato, multiple, max_qty=por_candidato,
                                       paso=paso)
        if base > 0:
            for candidato in candidatos:
                asignado[candidato['key']] = min(base, candidato['demand_qty'])
        restante = disponible - sum(asignado.values())

        paso = paso or multiple
        por_orden = sorted(candidatos, key=lambda c: c['sequence'])
        while restante >= paso:
            avance = False
            for candidato in por_orden:
                if restante < paso:
                    break
                if asignado[candidato['key']] + paso <= candidato['demand_qty'] + 1e-9:
                    asignado[candidato['key']] += paso
                    restante -= paso
                    avance = True
            if not avance:
                break
        return asignado

    # ------------------------------------------------------------------
    # Ordenamientos
    # ------------------------------------------------------------------

    @api.model
    def _sort_by_priority(self, candidatos, priority_ids):
        """Ordena según la lista de prioridad; los que no figuran, al final."""
        orden = {key: indice for indice, key in enumerate(priority_ids or [])}
        con_prioridad = [c for c in candidatos if c['key'] in orden]
        sin_prioridad = [c for c in candidatos if c['key'] not in orden]
        con_prioridad.sort(key=lambda c: orden[c['key']])
        sin_prioridad.sort(key=lambda c: c['sequence'])
        return con_prioridad + sin_prioridad

    @api.model
    def _sort_by_rotation(self, candidatos, priority_ids=None):
        """Ordena por venta diaria promedio, de mayor a menor.

        Si ningún candidato trae rotación, cae al orden de prioridad manual para que el
        resultado no quede indefinido.
        """
        if not any(c['ads'] for c in candidatos):
            _logger.info("Sin datos de rotación en los candidatos: se usa el orden de prioridad.")
            return self._sort_by_priority(candidatos, priority_ids)
        return sorted(candidatos, key=lambda c: (-c['ads'], c['sequence']))
