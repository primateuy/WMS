import glob as _glob
exec(open(BASE_SH + '/casos.py').read())
for _f in sorted(_glob.glob(BASE_SH + '/c[0-9]*.py')):
    exec(open(_f).read())
for nombre in CASOS:
    try:
        globals()['caso_' + nombre]()
    except Exception as e:
        import traceback; traceback.print_exc()
        env.cr.rollback()
        RESULTADOS.append((nombre.upper(), 'excepción armando el caso', False, str(e)[:200]))
        print(f'  [MAL] {nombre.upper()} · excepción armando el caso — {str(e)[:200]}')
    refrescar()
print('RESUMEN', sum(r[2] for r in RESULTADOS), 'OK de', len(RESULTADOS))
json.dump(RESULTADOS, open(BASE + '/log/resultado_' + '_'.join(CASOS) + '.json', 'w'), ensure_ascii=False, indent=1)
