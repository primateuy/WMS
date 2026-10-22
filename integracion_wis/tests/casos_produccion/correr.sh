#!/bin/bash
# Uso: correr.sh c02 c03 ...   (corre los casos dentro de odoo-bin shell contra la réplica)
S=$(cd "$(dirname "$0")" && pwd)
CONF=${CONF:-$S/casos.conf}
L=$(printf "'%s'," "$@")
cd /Users/darylyturraldelopez/Odoo/shared/odoo/community && { echo "BASE_SH='$S/sh'; CASOS=[${L%,}]"; cat $S/sh/run.py; } | /Users/darylyturraldelopez/Odoo/clients/forum/.venv/bin/python3.11 odoo-bin shell -c $CONF -d $(awk -F' = ' '/^db_name/{print $2}' $CONF) --no-http --logfile=$S/log/shell.log 2>&1 | grep -v "pkg_resources\|UserWarning"
