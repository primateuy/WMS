# E2E de webhooks: run_webhooks_e2e.py (contra Odoo en background).
# Benchmark de productos: bench_productos_wis.py (sin Odoo).
from . import test_integracion_productos
from . import test_conciliacion_stock_fases
from . import test_pedido_memo
from . import test_cola_pickings
from . import test_rendimiento_wis
