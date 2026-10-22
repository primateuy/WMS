#!/bin/bash
# Arma una réplica NUEVA desde la foto o17_wis_casos_base (no borra ninguna base: dropdb y
# rm -rf están denegados) y apunta casos.conf a ella. Uso: reset.sh [-u]   (-u: actualiza integracion_wis)
S=$(cd "$(dirname "$0")" && pwd)
CONF=${CONF:-$S/casos.conf}
pkill -f "odoo-bin -c $S/casos(_097)?\.conf" 2>/dev/null
for i in $(seq 1 30); do pgrep -f "odoo-bin -c $S/casos(_097)?\.conf" >/dev/null || break; sleep 1; done
DB="o17_wis_casos_$(date +%H%M%S)"
createdb -T o17_wis_casos_base -O odoo "$DB" || exit 1
F=~/Library/Application\ Support/Odoo/filestore
cp -cR "$F/o17_wis_casos_base" "$F/$DB"
sed -i '' "s/^db_name = .*/db_name = $DB/; s/^dbfilter = .*/dbfilter = ^$DB\$/" $CONF
echo "$DB" >> $S/log/bases_creadas.txt
: > $S/log/wis_requests.jsonl; : > $S/log/ucfe_requests.jsonl
if [ "$1" = "-u" ]; then
  (cd /Users/darylyturraldelopez/Odoo/shared/odoo/community && /Users/darylyturraldelopez/Odoo/clients/forum/.venv/bin/python3.11 odoo-bin -c $CONF -d $DB -u integracion_wis --stop-after-init --no-http --workers=0 --max-cron-threads=0 > $S/log/update_u.out 2>&1) || exit 1
fi
cd /Users/darylyturraldelopez/Odoo/shared/odoo/community && OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES no_proxy="*" NO_PROXY="*" nohup /Users/darylyturraldelopez/Odoo/clients/forum/.venv/bin/python3.11 odoo-bin -c $CONF > $S/log/server.out 2>&1 & echo $! > $S/log/server.pid
echo "réplica: $DB"
