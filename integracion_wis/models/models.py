from odoo import models, fields, api
from odoo.exceptions import ValidationError
import logging
_logger = logging.getLogger(__name__)
import datetime;
import requests;
import hashlib;
import random;


class IntegracionWIS(models.Model):
    _name = 'integracion_wis.integracion_wis'
    _description = 'Configuración de la API de WIS para la correcta integración'
    _check_company_auto = True

    client_id = fields.Char(
        string="Cliente ID",
        required=True
    )

    empresa_id = fields.Integer(
        string="ID de Empresa en WIS",
        required=True
    )

    url_access_token = fields.Char(
        string="URL ACCESS TOKEN",
        required=True
    )
    
    client_secret = fields.Char(
        string="Client Secret",
        required=True
    )
    
    apiLink = fields.Char(
        string='Enlace de la API',
        help='Enlace base de la API de WIS para realizar las solicitudes',
        required=True,
    )

   

    token = fields.Char(
        string='api token'
    )

    expiracionToken = fields.Datetime(string="Fecha de expiración del token")


    company_id = fields.Many2one(
        'res.company',
        string='Compañía',
        default=lambda self: self.env.company,
        required=True,
        ondelete='cascade',
        help='Compañía para la cual se configura la integración con WIS'
    )

    ubicacionesAConsultar = fields.Many2many(
    'stock.location',
    string='Ubicaciones a Consultar',
    domain=[('usage', '=', 'internal')])
    
    diferenciaMinima = fields.Integer(
        string='Diferencia Mínima',
        default=1,
        help='Diferencia mínima para registrar un ajuste de stock.'
    )

    ubicacionReponerStock = fields.Many2one('stock.location', string='Ubicación para Reponer Stock', domain=[('usage', '=', 'internal')])

    ultima_sync_productos = fields.Datetime(
        string='Última sincronización de productos',
        readonly=True,
        help='Fecha de la última conciliación masiva de productos completada exitosamente. '
             'Solo se sincronizan productos cuyo write_date sea posterior a este valor.',
    )

    comunicacion_activa = fields.Boolean(
        string='Comunicación con WIS habilitada',
        default=True,
        help='Si está deshabilitada, Odoo se comporta con su lógica por defecto sin enviar '
             'ninguna comunicación al WMS. Los crons, triggers automáticos y botones '
             'manuales de integración quedan inactivos.',
    )

    # Webhook ajustes (spec sección 6.4)
    picking_type_ajuste_wis_id = fields.Many2one(
        'stock.picking.type',
        string='Tipo de operación para ajustes WIS',
        help='Tipo de operación usado al crear los pickings que reflejan ajustes recibidos '
             'desde el webhook ajustes (entradas). Para los ajustes negativos (salidas) se '
             'usa el return_picking_type_id de este tipo.'
    )

    tipo_ajuste_recuento = fields.Char(
        string='Código TipoAjuste de recuento físico',
        help='Valor exacto del campo TipoAjuste que WIS envía cuando el ajuste corresponde a '
             'un recuento físico. Si el TipoAjuste recibido coincide con este código, el handler '
             'modifica directamente stock.quant vía action_apply_inventory. Cualquier otro '
             'TipoAjuste se procesa como movimiento de stock (picking de transferencia interna). '
             'PENDIENTE: confirmar con WIS el catálogo completo de TipoAjuste y el código exacto '
             'del recuento físico.'
    )

    _sql_constraints = [
        ('company_unique', 'unique(company_id)', '¡Solo puede existir una configuración por compañía!'),
    ]

    @api.model
    def _comunicacion_habilitada(self):
        config = self.search([], limit=1)
        return bool(config and config.comunicacion_activa)

    def _get_clean_api_url(self):
        self.ensure_one()
        url = (self.apiLink or '').strip()
        while url.endswith('/index.html') or url.endswith('/'):
            if url.endswith('/index.html'):
                url = url[:-11]
            else:
                url = url[:-1]
        return url

    def renovarToken(self):
        if not self.url_access_token or not self.client_id or not self.client_secret:
            raise ValidationError("Datos no válidos para la renovación del token")

        headers = {
            'Content-Type': 'application/x-www-form-urlencoded'
        }

        body = {
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'scope': 'api',
            'grant_type': 'client_credentials'
        }

        req = requests.post(self.url_access_token, headers=headers, data=body)

        if req.status_code == 200:
            reqJson = req.json()

            self.token = reqJson.get('access_token')
            expires_in = reqJson.get('expires_in') or 3600
            self.expiracionToken = datetime.datetime.now() + datetime.timedelta(seconds=expires_in)

        else:
            raise ValidationError(f"Hubo un problema en la request: {req.text}")

        


    def consultarAPI(self, link, params, body, method='GET'):

        if not self.apiLink or not self.client_id or not self.client_secret or not self.url_access_token or not self.empresa_id:
            raise ValidationError("Faltan datos para acceder a la API")
                
        if not self.token or self.expiracionToken < datetime.datetime.now():
            self.renovarToken()

        headers = {
            "Content-Type": "application/json",
            "accept-language": "es",
            "Authorization": f"Bearer {self.token}" 
        }

        _logger.info("Llega hasta los headers")

        api_url = self._get_clean_api_url()

        try:
            req = requests.request(url=api_url + link, method=method, json=body, headers=headers, params=params, timeout=30)
            _logger.info(f"REQ: {req.text}")
            _logger.info(f"JSON ES BODY => {body}")
            if req.status_code != 200:
                _logger.error(f"Error en la API: {req.status_code} - {req.text}")
                raise ValidationError(f"Error en la API: {req.status_code} - {req.text}")

            try:
                return req.json()
            except ValueError as e:
                _logger.error(f"Error al decodificar JSON: {str(e)}")
                raise ValidationError(f"Error al decodificar la respuesta de la API: {req.text}")

        except Exception as e:
            _logger.error(f"Error en la solicitud a la API: {str(e)}")
            raise ValidationError(f"Error en la solicitud a la API: {str(e)}")




        
        

    def actualizarStock(self, vals):
        if not self.apiLink or not self.client_id or not self.client_secret or not self.url_access_token:
            raise ValidationError("Faltan datos para acceder a la API");
            
        if not self.token or self.expiracionToken < datetime.datetime.now():
            self.renovarToken();

            

        payload = {
            "empresa": self.empresa_id,
            "productos": [{
                    "codigoProducto": vals.codigo_unico
            }]
        }

        response = self.consultarAPI(
            link="/Producto/CreateOrUpdate",
            params=payload,
            method="POST",
            body=None
        )


        vals.qty_available = response['cantidadGenerica']
        vals.with_context(_avoid_wms=True).write({'qty_available': vals.qty_available})

        _logger.info(f"El usuario: {self.company_id.id} actualizó el stock del producto {vals.name} - {vals.id} - {vals.codigo_unico} a {vals.qty_available}, en el día {datetime.datetime.now()}");
        return True;


    def _build_producto_payload(self, vals):
        """Construye el dict de un producto para /Producto/CreateOrUpdate.

        manejoIdentificador y tipoManejoFecha son campos de creación:
        WIS no permite modificarlos si el producto ya tiene movimientos.
        Solo se incluyen cuando el producto todavía no tiene codigo_unico
        (primera sincronización).
        """
        nombre = vals.name[:65] if len(vals.name) > 65 else vals.name
        unidad_wis = (vals.uom_id.wis_code or '').strip() or 'UND'
        payload = {
            "codigoProducto": vals.codigo_unico,
            "codigo":         vals.codigo_unico,
            "descripcion":    nombre,
            "familia":        1,
            "unidadMedida":   unidad_wis,
            "clase":          1,
            "ramo":           1,
            "pesoNeto":       vals.weight,
            "precioVenta":    vals.list_price,
            "categoria1":     vals.categ_id.name if vals.categ_id else "",
            "unidadBulto":    1,
            "activo":         vals.active,
        }
        # Solo en la creación inicial (producto nuevo en WIS)
        if not vals.codigo_unico:
            tracking = vals.tracking
            payload["tipoManejoFecha"]    = 'F' if tracking in ('lot', 'serial') else 'D'
            payload["manejoIdentificador"] = 'L' if tracking in ('lot', 'serial') else 'P'
        return payload

    def _enviar_chunk_productos(self, chunk, lote_label):
        """Envía un chunk de productos. Si falla el batch, reintenta uno por uno
        para que un producto con error no bloquee al resto del lote."""
        payload = {
            "empresa":      self.empresa_id,
            "dsReferencia": f"Sincronización masiva desde Odoo - {lote_label}",
            "productos":    chunk,
        }
        try:
            self.consultarAPI(link="/Producto/CreateOrUpdate", body=payload,
                              params=None, method="POST")
            return len(chunk), 0, []
        except Exception as batch_err:
            _logger.warning(
                "[WIS] %s | batch falló (%s), reintentando uno por uno...",
                lote_label, str(batch_err)
            )

        # Fallback: reintento individual para aislar el producto problemático
        enviados, errores, errores_detalle = 0, 0, []
        for item in chunk:
            single_payload = {
                "empresa":      self.empresa_id,
                "dsReferencia": f"Reintento individual - {item.get('codigo', '?')}",
                "productos":    [item],
            }
            try:
                self.consultarAPI(link="/Producto/CreateOrUpdate", body=single_payload,
                                  params=None, method="POST")
                enviados += 1
            except Exception as item_err:
                err_str = str(item_err)
                # WIS no permite modificar manejoIdentificador en productos con
                # movimientos, aunque no lo enviemos (aplica un default y compara).
                # Este caso NO es un error real: el producto ya existe en WIS
                # con el valor correcto — simplemente se omite.
                if 'ManejoIdentificador' in err_str and 'No se permite modificar' in err_str:
                    enviados += 1
                    _logger.warning(
                        "[WIS] reintento individual OMITIDO (manejoIdentificador bloqueado) "
                        "| codigo=%s — producto ya sincronizado en WIS",
                        item.get('codigo', '?')
                    )
                else:
                    errores += 1
                    errores_detalle.append(
                        f"{item.get('codigo', '?')}: {err_str}"
                    )
                    _logger.error("[WIS] reintento individual ERROR | codigo=%s | %s",
                                  item.get('codigo', '?'), err_str)
        return enviados, errores, errores_detalle

    def insertarProductosMasivo(self, variantes, chunk_size=200):
        """Envía todos los productos en lotes a /Producto/CreateOrUpdate.

        En lugar de 1 llamada HTTP por producto, agrupa hasta chunk_size
        productos por request. Para 1400 productos con chunk_size=200
        se realizan 7 llamadas en lugar de 1400.

        Si un chunk falla (ej. un producto con manejoIdentificador bloqueado),
        reintenta cada producto individualmente para no perder los demás.

        Returns dict con 'enviados', 'errores' y 'errores_detalle'.
        """
        variantes_list = list(variantes)
        total = len(variantes_list)
        enviados = 0
        errores = 0
        todos_errores_detalle = []
        total_chunks = -(-total // chunk_size)  # ceil division

        _logger.info("[WIS] insertarProductosMasivo | total=%d | chunk_size=%d | chunks=%d",
                     total, chunk_size, total_chunks)

        for chunk_idx in range(0, total, chunk_size):
            chunk_vars = variantes_list[chunk_idx:chunk_idx + chunk_size]
            lote_num = chunk_idx // chunk_size + 1
            lote_label = f"lote {lote_num}/{total_chunks}"

            productos_payload = [
                self._build_producto_payload(v)
                for v in chunk_vars
                if v.codigo_unico
            ]

            if not productos_payload:
                continue

            ok, err, err_detalle = self._enviar_chunk_productos(productos_payload, lote_label)
            enviados += ok
            errores  += err
            todos_errores_detalle.extend(err_detalle)

            _logger.info("[WIS] insertarProductosMasivo | %s | ok=%d err=%d | acumulado=%d",
                         lote_label, ok, err, enviados)

        _logger.info("[WIS] insertarProductosMasivo | FINALIZADO | enviados=%d | errores=%d",
                     enviados, errores)
        return {'enviados': enviados, 'errores': errores,
                'errores_detalle': todos_errores_detalle}

    def insertarProducto(self, vals):
        

        productos = [];
        unidad_wis = (vals.uom_id.wis_code or '').strip() if vals.uom_id else 'UND'
        unidad_wis = unidad_wis or 'UND'
        numeroRandom = random.randint(100000, 999999);

        _logger.info("Nombre del producto => {}".format(vals.name));
        _logger.info("DISPLAY NAME => {}".format(vals.display_name));

        if len(vals.name) > 65:
            _logger.info("El producto supera los 65 caracteres, se truncará para la integración con WIS");
            vals.name = vals.name[:65];
        codigo = ''
        if vals.codigo_unico:
            codigo = vals.codigo_unico;
        else:
            codigo = f"PRD-{numeroRandom}"

            
        tracking = vals.tracking
        tipo_manejo_fecha    = 'F' if tracking in ('lot', 'serial') else 'D'
        manejo_identificador = 'L' if tracking in ('lot', 'serial') else 'P'

        productos = [{
                "codigoProducto": codigo,
                "codigo": codigo,
                "descripcion": f"{vals.name}",
                "familia": 1,
                "unidadMedida": unidad_wis,
                "clase": 1,
                "ramo": 1,
                "pesoNeto": vals.weight,
                "manejoIdentificador": manejo_identificador,
                "tipoManejoFecha": tipo_manejo_fecha,
                "precioVenta": vals.list_price,

                "categoria1": vals.categ_id.name if vals.categ_id else "",
                "unidadBulto": 1,
                "activo": vals.active

            }]

        

        _logger.info("Payload del producto a enviar => {}".format(productos));
        

        


        payload = {
            "empresa": self.empresa_id,
            "dsReferencia": f"PRODUCTO: {vals.display_name} desde Odoo",
            "productos": productos
        }


        

        response = self.consultarAPI(
            link="/Producto/CreateOrUpdate",
            body=payload,
            params=None,
            method="POST"
        )

        response['codigoUnico'] = codigo;

        return response



    
    def eliminarCodigoBarra(self, vals, codigoBarra):
        
        codigos = [{
            "codigo": codigoBarra,
            "producto": vals.codigo_unico,
            "tipoCodigo": 1,
            "prioridadUso": 1,
            "cantidadEmbalaje": 1,
            "tipoOperacion": "B"
        }]

        payload = {
            "empresa": self.empresa_id,
            "dsReferencia": f"CODIGO DE BARRA DE {vals.display_name} agregado desde Odoo",
            "archivo": "Archivo",
            "codigosDeBarras": codigos
        }

        response = self.consultarAPI(
            link="/CodigoBarras/CreateUpdateOrDelete",
            body=payload,
            params=None,
            method="POST"
        )


        return response


    def existeBarcode(self, vals):
        if not self.apiLink or not self.client_id or not self.client_secret or not self.url_access_token:
            raise ValidationError("Faltan datos para acceder a la API");
            
        if not self.token or self.expiracionToken < datetime.datetime.now():
            self.renovarToken();

        params = {
            "empresa": self.empresa_id,
            "codigo": int(vals.barcode)
        }



        _logger.info("Realizando consulta de existencia de código de barras en WIS %s", params);

        api_url = self._get_clean_api_url()

        req = requests.get(
            url=f"{api_url}/CodigoBarras/GetCodigoBarras",
            headers={
                "Authorization": f"Bearer {self.token}"
            },
            params=params
        )

        if req is not None:
            _logger.info("El código de barras existe en WIS %s", req);
            return True;

        _logger.info("El código de barras NO existe en WIS %s", req);
        return False;

    def insertarBarcode(self, vals):

        existeBarcodeBool = self.existeBarcode(vals);
        codigos = [{
            "codigo": vals.barcode,
            "producto": vals.codigo_unico,
            "tipoCodigo": 13,
            "prioridadUso": 1,
            "cantidadEmbalaje": 1,
            "tipoOperacion": "A" if not existeBarcodeBool else "S"
        }]

        payload = {
            "empresa": self.empresa_id,
            "dsReferencia": f"CODIGO DE BARRA DE {vals.display_name} agregado desde Odoo",
            "archivo": "Archivo",
            "codigosDeBarras": codigos
        }

        response = self.consultarAPI(
            link="/CodigoBarras/CreateUpdateOrDelete",
            body=payload,
            params=None,
            method="POST"
        )


        return response


    def transferirStock(self, vals):
        transferencias = []

        for i in vals.move_ids:
            transferencias.append({
                "ubicacion": vals.location_id.name,
                "ubicacionDestino": vals.location_dest_id.name,
                "codigoProducto": i.product_id.codigo_unico,
                "identificador": i.product_id.codigo_unico,
                "cantidad": i.product_uom_qty
            })


        _logger.info("TRANSFERENCIAS: %s", transferencias);
        _logger.info("DESDE STOCK TRANSFERENCIAS");


        payload = {
            "empresa": self.empresa_id,
            "dsReferencia": f"Transferencia de stock: {vals.id}",
            "archivo": "Archivo",
            "transferencias": transferencias
        }

        response = self.consultarAPI(
            link="/Stock/Transferir",
            body=payload,
            params=None,
            method="POST"
        )


        if response.status == 200:
            _logger.info("Se ejecuto la funcion correctamente", response.json());
    
        else:
            _logger.info("Ocurrio un error en transferir el stock");





    def insertarClienteOrSupplier(self, vals, tipo):

        self.env['logs.res.partner'].create({
            'partner_id': vals.id,
            'fecha': fields.Datetime.now(),
            'texto': 'Empezando operación',
        })



        numeroRandom = random.randint(100000, 999999);
        name_clean = (vals.name or "").upper()
        street_clean = (vals.street or "").upper()
        city_clean = (vals.city or "").upper()
        phone_clean = (vals.phone or "").replace(" ", "").replace("-", "")

        country_code = "UY"  
        if vals.country_id and vals.country_id.code:
            country_code = vals.country_id.code.upper()

        punto_entrega_parts = []
    
        if vals.street:
            punto_entrega_parts.append(f"Calle: {vals.street}")
        if vals.street2:
            punto_entrega_parts.append(f"Esquina: {vals.street2}")
        
        if vals.city:
            ciudad_codigo = vals.city
            if vals.zip:
                ciudad_codigo += f" (CP: {vals.zip})"
            punto_entrega_parts.append(ciudad_codigo)
        elif vals.zip:
            punto_entrega_parts.append(f"CP: {vals.zip}")
        
        if vals.state_id and vals.state_id.name:
            punto_entrega_parts.append(f"Provincia: {vals.state_id.name}")
        
        if hasattr(vals, 'barrio') and vals.barrio:
            punto_entrega_parts.append(f"Barrio: {vals.barrio}")
        elif hasattr(vals, 'l10n_uy_barrio') and vals.l10n_uy_barrio:
            punto_entrega_parts.append(f"Barrio: {vals.l10n_uy_barrio}")
        
        if hasattr(vals, 'ref') and vals.ref:
            punto_entrega_parts.append(f"Ref: {vals.ref}")
        
        punto_entrega = " - ".join(punto_entrega_parts) if punto_entrega_parts else "Sin dirección especificada"
        
        punto_entrega = punto_entrega[:120]  

        if tipo == 'CLI':
            codigoAgente = vals.codigo_unico_cliente or f"CLI-{numeroRandom}"
        else:
            codigoAgente = vals.codigo_unico_proveedor or f"PRO-{numeroRandom}"

        agentes = [{
            "codigoAgente": codigoAgente,
            "tipo": tipo,
            "descripcion": name_clean,
            "estado": 15,
            "anexo1": "",
            "anexo2": "",
            "anexo3": "",
            "anexo4": "",
            "barrio": "",
            "direccion": punto_entrega,
            "aceptaDevolucion": "S",
            "telefonoPrincipal": phone_clean or "00000000",
            "telefonoSecundario": "",
            "valorManejoVidaUtil": 0,
            "categoria": "",
            "codigoPostal": vals.zip or "",
            "grupoConsulta": "",
            "puntoDeEntrega": "",
            "idClienteFilial": "C",
            "tipoFiscal": "RUT",
            "caracteristicaTelefonica": "",
            "otroDatoFiscal": "",
            "ordenDeCarga": 1,
            "pais": country_code, 
            "subdivision": "",
            "localidad": city_clean
        }]



        payload = {
            "empresa": self.empresa_id,
            "dsReferencia": f"Creación de agente desde Odoo: {name_clean}",
            "archivo": "Archivo",
            "agentes": agentes
        }

        try: 
            response = self.consultarAPI(
            link="/Agente/CreateOrUpdate",
            body=payload,
            params=None,
            method="POST"
            )

            response['codigoUnico'] = codigoAgente;

        except Exception as e:
            raise ValidationError(f"Error al enviar la información a WIS: {str(e)}")

        

        return response

    def getPedido(self, vals):

        if not vals:
            raise ValidationError("No se ha encontrado información del pedido");

        payload = {
            "empresa": self.empresa_id,
            "numero": vals.codigo_unico,
            "tipoAgente": "CLI",
            "codigoAgente": vals.partner_id.codigo_unico_cliente,
        }


        try:
            response = self.consultarAPI(
                link="/Pedido/GetPedido",
                body=None,
                params=payload,
                method="GET"
            )
            _logger.info("RESPUESTA DEL GET PEDIDO: %s", str(response))
        except Exception as e:
            _logger.error("Error en la solicitud a la API: %s", str(e))
            raise ValidationError(f"Error en la solicitud a la API: {str(e)}")




        return response;


   

    def insertarPedidos(self, vals, tipo):
        
        _logger.info(f"El pedido del tipo es => {tipo}");

        if not tipo:
            raise ValidationError("Debe especificar el tipo de pedido que se va a insertar en WMS");
        if vals.codigo_unico and vals.picking_type_id != 'creacion_actualiacion':
            raise ValidationError("El picking ya tiene un código único asignado, no se puede volver a enviar a WMS");



        if not vals:
            raise ValidationError("No se ha encontrado información del pedido");

        detalles = [];
        for prod in vals.move_ids:
            if not prod.product_id.codigo_unico:
                raise ValidationError(f"El producto '{prod.product_id.name}' no tiene un código WMS asignado. Sincronícelo primero desde el formulario del producto.")
            detalles.append({
                "codigoProducto": prod.product_id.codigo_unico,
                "identificador": "*",
                "cantidad": prod.product_uom_qty
            })

        _logger.info(f"TERMINANDO DE ASIGNAR DETALLES detalles: {detalles}")

        direccion = f'{vals.location_dest_id.name}';

        if vals.picking_type_id.code and vals.location_id and vals.location_dest_id:
            direccion = f'ORIGEN: {vals.location_id.name} - DESTINO {vals.location_dest_id.name}';


        
        direccion = vals.location_dest_id.name;


        _partner = vals._get_wis_partner()
        tipo_agente = vals.picking_type_id.tipo_agente_wis or 'CLI'
        codigo_agente = (_partner.codigo_unico_cliente if tipo_agente == 'CLI' else _partner.codigo_unico_proveedor) if _partner else ''

        # Código único estandarizado por tipo: W-P-<id del picking> (pedido).
        nro_pedido = vals.codigo_unico or f"W-P-{vals.id}"

        pedido = {
            "tipoExpedicion": vals.picking_type_id.tipo_expedicion_wis or ("WSF" if tipo == 'NORM' else "WIS"),
            "nroPedido": nro_pedido,
            "comparteContenedorEntrega": vals.name,
            "codigoAgente": codigo_agente,
            "tipoAgente": tipo_agente,
            "fechaEntrega": vals.scheduled_date.isoformat(),
            "tipoPedido": tipo,
            "direccion": direccion,
            "detalles": detalles,
        }

        if tipo == 'FINT' and vals.wms_nro_caja:
            pedido["lpns"] = [{
                "idExterno": vals.wms_nro_caja,
                "tipo": "FINTEMP",
            }]

        pedidos = [pedido]




        payload = {
            "empresa": self.empresa_id,
            "dsReferencia": f"Creación de Pedido desde Odoo: {vals.name}",
            "archivo": "Archivo",
            "pedidos": pedidos,
            
        }

        response = self.consultarAPI(
            link="/Pedido/Create",
            body=payload,
            params=None,
            method="POST"
        )

        response['codigoUnico'] = nro_pedido
        

        _logger.info(f"RESPONSE => {response}")
        return response;


    def insertarDevolucion(self, picking):
            moves = picking.move_ids or (hasattr(picking, 'move_ids_without_package') and picking.move_ids_without_package) or []

            if not moves:
                _logger.info("No hay productos detectados en la devolución %s", picking.name)
                return False

            # Fecha de vencimiento por defecto: WIS la exige para productos perecederos (no
            # duraderos). Se busca la real en el lote/línea; si no hay, se usa la programada del
            # picking, y como último recurso hoy+365 (mismo criterio que insertarReferenciaRecepcion).
            if picking.scheduled_date:
                fecha_venc_default = picking.scheduled_date.date().isoformat()
            else:
                fecha_venc_default = (datetime.datetime.now() + datetime.timedelta(days=365)).date().isoformat()

            detalles = []
            for move in moves:
                codigo_producto = move.product_id.codigo_unico or ''

                # Vencimiento real desde las líneas de movimiento (lotes), si existe.
                fecha_venc = None
                for ml in move.move_line_ids:
                    if hasattr(ml, 'expiration_date') and ml.expiration_date:
                        fecha_venc = ml.expiration_date.date().isoformat()
                        break
                    if hasattr(ml, 'lot_id') and ml.lot_id and getattr(ml.lot_id, 'expiration_date', False):
                        fecha_venc = ml.lot_id.expiration_date.date().isoformat()
                        break

                detalles.append({
                    'idLineaSistemaExterno': f"odoo__stock.move__{move.id}",
                    'codigoProducto': codigo_producto,
                    'cantidadReferencia': move.product_uom_qty,
                    'fechaVencimiento': fecha_venc or fecha_venc_default,
                })

            # Código único estandarizado por tipo: W-D-<id del picking> (devolución).
            codigo = picking.codigo_unico or f"W-D-{picking.id}"
            _tipo_ag_dev = picking.picking_type_id.tipo_agente_wis or 'CLI'
            _partner_dev = picking._get_wis_partner()
            _cod_ag_dev = ''
            if _partner_dev:
                _cod_ag_dev = (_partner_dev.codigo_unico_cliente if _tipo_ag_dev == 'CLI' else _partner_dev.codigo_unico_proveedor) or (picking.partner_id.vat if picking.partner_id else '')
            payload = {
                'empresa': self.empresa_id,
                'dsReferencia': f"DEVOLUCIÓN DE CLIENTE DESDE ODOO: {picking.name}",
                'referencias': [{
                    'referencia': codigo,
                    # tipoReferencia sale del tipo de operación (Tipo de Pedido WIS): para caja
                    # cerrada fin de temporada es 'ODFT'; 'OD' (Orden de Devolución) si no está.
                    'tipoReferencia': picking.picking_type_id.tipo_pedido_wis or 'OD',
                    'codigoAgente': _cod_ag_dev,
                    'tipoAgente': _tipo_ag_dev,
                    'predio': '1',
                    'fechaEstimada': picking.scheduled_date.isoformat() if picking.scheduled_date else None,
                    'observaciones': f"Devolución desde ubicación: {picking.location_id.name}",
                    'detalles': detalles
                }]
            }

            response = self.consultarAPI(
                link="/ReferenciaRecepcion/Create",
                body=payload,
                params=None,
                method="POST"
            )

            response['codigoUnico'] = codigo
            return response

    def insertarLpns(self, picking):
        """Crea en WIS los LPN (cajas) de una devolución de caja cerrada fin de temporada.

        Endpoint: POST /Lpn/Create (spec WIS-WMS API 10.2 §16.1). Crea UN LPN por cada
        `stock.quant.package` presente en las move_lines del picking; sus `detalles` llevan
        el contenido declarado de la caja (producto + cantidad + lote + vencimiento).

        - idExterno = `package.wis_id_externo` o `package.name`.
        - tipo = `picking_type_id.tipo_lpn_wis` (default 'FINTEMP'); si está vacío se omite y
          WIS aplica su parámetro IE_535_TP_LPN_TIPO.
        - cantidadDeclarada = `move_line.quantity` (en 'assigned' ya refleja la caja reservada;
          las cajas viajan por la cadena → el incoming trae `move_line.package_id` del
          predecesor ya en 'assigned', verificado en el flujo 2270->2271).
        - idPacking = número de la referencia de recepción (la misma `referencia` que se envió
          en `insertarDevolucion`), para agrupar los LPN bajo esa referencia. La referencia se
          manda SIEMPRE primero (enviarWS: insertarDevolucion y luego los LPN).

        Fallback (picking sin paquetes): un único LPN con idExterno = `wms_nro_caja` o el
        `codigo_unico`/W-D- y detalles desde `move_ids` (cantidad = product_uom_qty).
        """
        tipo_lpn = (picking.picking_type_id.tipo_lpn_wis or '').strip()
        # Número de la referencia de recepción (idéntico a la `referencia` de insertarDevolucion).
        ref_recepcion = picking.codigo_unico or f"W-D-{picking.id}"
        fecha_venc_default = (datetime.datetime.now() + datetime.timedelta(days=365)).date().isoformat()

        def _fecha_venc(ml):
            """Vencimiento real desde la move_line/lote; None si no aplica."""
            if hasattr(ml, 'expiration_date') and ml.expiration_date:
                return ml.expiration_date.date().isoformat()
            if hasattr(ml, 'lot_id') and ml.lot_id and getattr(ml.lot_id, 'expiration_date', False):
                return ml.lot_id.expiration_date.date().isoformat()
            return None

        # Agrupar move_lines por paquete (la caja cerrada). Prioriza el paquete origen
        # (package_id, el que llega por la cadena); si no, el destino (result_package_id).
        lineas_por_paquete = {}   # paquete -> [move_line, ...]
        sin_paquete = []
        for ml in picking.move_line_ids:
            if (ml.quantity or 0) <= 0:
                continue
            paquete = ml.package_id or ml.result_package_id
            if paquete:
                lineas_por_paquete.setdefault(paquete, []).append(ml)
            else:
                sin_paquete.append(ml)

        lpns = []

        def _detalles_desde_move_lines(move_lines):
            """Consolida move_lines por (producto, lote) en detalles de LPN."""
            agrupado = {}  # (product_id, lot_id) -> dict detalle acumulado
            for ml in move_lines:
                codigo_producto = ml.product_id.codigo_unico or ''
                if not codigo_producto:
                    raise ValidationError(
                        f"No se encontró código único WIS en el producto: {ml.product_id.name}. "
                        f"Sincronícelo con WIS antes de crear el LPN.")
                clave = (ml.product_id.id, ml.lot_id.id if ml.lot_id else False)
                det = agrupado.get(clave)
                if not det:
                    det = {
                        'idLineaSistemaExterno': f"odoo__stock.move.line__{ml.id}",
                        'codigoProducto': codigo_producto,
                        'cantidadDeclarada': 0.0,
                        'fechaVencimiento': _fecha_venc(ml) or fecha_venc_default,
                    }
                    if ml.lot_id:
                        det['identificador'] = ml.lot_id.name
                    agrupado[clave] = det
                det['cantidadDeclarada'] += ml.quantity
            return list(agrupado.values())

        # Caso principal: un LPN por paquete.
        for paquete, move_lines in lineas_por_paquete.items():
            lpn = {
                'idExterno': paquete.wis_id_externo or paquete.name,
                'idPacking': ref_recepcion,
                'detalles': _detalles_desde_move_lines(move_lines),
            }
            if tipo_lpn:
                lpn['tipo'] = tipo_lpn
            lpns.append(lpn)

        # Fallback: el picking no tiene cajas modeladas como paquetes.
        if not lpns:
            detalles_fb = []
            fuente = sin_paquete or picking.move_line_ids
            if fuente:
                detalles_fb = _detalles_desde_move_lines(fuente)
            else:
                for move in picking.move_ids:
                    codigo_producto = move.product_id.codigo_unico or ''
                    if not codigo_producto:
                        raise ValidationError(
                            f"No se encontró código único WIS en el producto: {move.product_id.name}.")
                    detalles_fb.append({
                        'idLineaSistemaExterno': f"odoo__stock.move__{move.id}",
                        'codigoProducto': codigo_producto,
                        'cantidadDeclarada': move.product_uom_qty,
                        'fechaVencimiento': fecha_venc_default,
                    })
            if not detalles_fb:
                _logger.info("[WIS] insertarLpns: picking %s sin líneas para LPN", picking.name)
                return False
            id_externo = picking.wms_nro_caja or picking.codigo_unico or f"W-D-{picking.id}"
            lpn = {'idExterno': id_externo, 'idPacking': ref_recepcion, 'detalles': detalles_fb}
            if tipo_lpn:
                lpn['tipo'] = tipo_lpn
            lpns.append(lpn)

        payload = {
            'empresa': self.empresa_id,
            'dsReferencia': f"Creación de LPN desde Odoo: {picking.name}",
            'lpns': lpns,
        }

        response = self.consultarAPI(
            link="/Lpn/Create",
            body=payload,
            params=None,
            method="POST"
        )
        _logger.info("[WIS] insertarLpns: %s LPN(s) creados para %s", len(lpns), picking.name)
        return response

    def insertarReferenciaRecepcion(self, picking):

        moves = picking.move_ids or (hasattr(picking, 'move_ids_without_package') and picking.move_ids_without_package) or []

        if not moves:
            self.env['wms.integracion.log'].create({
                'fecha': fields.Datetime.now(),
                'nivel': 'error',
                'modelo': 'stock.picking',
                'texto': f"No se detectaron líneas en el picking {picking.name} (state: {picking.state})",
                'picking_id': picking.id,
                'resultado': 'error',
                'detalle': 'move_ids vacío al intentar insertar referencia recepción'
            })
            return False

        
        if picking.scheduled_date:
            fecha_venc_default = picking.scheduled_date.date().isoformat()
        else:
            fecha_venc_default = (datetime.datetime.now() + datetime.timedelta(days=365)).date().isoformat()

        detalles = []
        colocarFecha = False;
        for move in moves:
            codigo_producto = move.product_id.codigo_unico or ''

            if not codigo_producto:
                raise ValidationError(f"No se encontró código único WIS en el producto: {move.product_id.name}. Por favor, asegúrese de que el producto esté sincronizado con WIS antes de enviar el movimiento.")

            # Buscar fecha de vencimiento real en líneas de movimiento (lotes)
            fecha_venc = None
            for ml in move.move_line_ids:
                if hasattr(ml, 'expiration_date') and ml.expiration_date:
                    fecha_venc = ml.expiration_date.date().isoformat()
                    break
                if hasattr(ml, 'lot_id') and ml.lot_id and hasattr(ml.lot_id, 'expiration_date') and ml.lot_id.expiration_date:
                    fecha_venc = ml.lot_id.expiration_date.date().isoformat()
                    break

            detalles.append({
                'idLineaSistemaExterno': f"odoo__stock.move__{move.id}",
                'codigoProducto': codigo_producto,
                'cantidadReferencia': move.product_uom_qty,
                'fechaVencimiento': fecha_venc or fecha_venc_default,
            })

            colocarFecha = True if fecha_venc else False;

        # Código único estandarizado por tipo: W-R-<id del picking> (recepción).
        codigo = picking.codigo_unico or f"W-R-{picking.id}"
        tipo_agente = picking.picking_type_id.tipo_agente_wis or 'PRO'
        _partner_rec = picking._get_wis_partner()
        codigo_agente = (_partner_rec.codigo_unico_cliente if tipo_agente == 'CLI' else _partner_rec.codigo_unico_proveedor) if _partner_rec else ''

        if tipo_agente == 'CLI' and not codigo_agente:
            self.env['wms.integracion.log'].create({
                'fecha': fields.Datetime.now(),
                'nivel': 'error',
                'modelo': 'stock.picking',
                'texto': f"El cliente asociado al picking {picking.name} no tiene un código único de cliente para WIS. Por favor, sincronice el cliente con WIS antes de enviar la referencia de recepción.",
                'picking_id': picking.id,
                'resultado': 'error',
                'detalle': 'El cliente asociado al picking no tiene un código único de cliente para WIS. Por favor, sincronice el cliente con WIS antes de enviar la referencia de recepción.'
            })

            raise ValidationError(f"El cliente asociado al picking {picking.name} no tiene un código único de cliente para WIS. Por favor, sincronice el cliente con WIS antes de enviar la referencia de recepción.")

        if tipo_agente == 'PRO' and not codigo_agente:
            self.env['wms.integracion.log'].create({
                'fecha': fields.Datetime.now(),
                'nivel': 'error',
                'modelo': 'stock.picking',
                'texto': f"El proveedor asociado al picking {picking.name} no tiene un código único de proveedor para WIS. Por favor, sincronice el proveedor con WIS antes de enviar la referencia de recepción.",
                'picking_id': picking.id,
                'resultado': 'error',
                'detalle': 'El proveedor asociado al picking no tiene un código único de proveedor para WIS. Por favor, sincronice el proveedor con WIS antes de enviar la referencia de recepción.'
            })

            raise ValidationError(f"El proveedor asociado al picking {picking.name} no tiene un código único de proveedor para WIS. Por favor, sincronice el proveedor con WIS antes de enviar la referencia de recepción.")



        payload = {
            'empresa': self.empresa_id,
            'dsReferencia': f"RECEPCIÓN DESDE ODOO: {picking.name}",
            'referencias': [{
                'referencia': codigo,
                # tipoReferencia sale del tipo de operación (Tipo de Pedido WIS); 'OC' si no está.
                'tipoReferencia': picking.picking_type_id.tipo_pedido_wis or 'OC',
                'fechaVencimientoOrden': picking.date_done.isoformat() if (colocarFecha and picking.date_done) else None,
                'codigoAgente': codigo_agente,
                'tipoAgente': tipo_agente,
                'predio': '1',
                'fechaEstimada': picking.scheduled_date.isoformat() if picking.scheduled_date else None,
                'detalles': detalles
            }]
        }

        response = self.consultarAPI(
            link="/ReferenciaRecepcion/Create",
            body=payload,
            params=None,
            method="POST"
        )

        response['codigoUnico'] = codigo
        return response

    def actualizarReferenciaRecepcion(self, picking):
        detalles = []
        for move in picking.move_ids:
            codigo_producto = move.product_id.codigo_unico or ''
            if not codigo_producto:
                raise ValidationError(f"No se encontró código único WIS en el producto: {move.product_id.name}.")
            detalles.append({
                'idLineaSistemaExterno': f"odoo__stock.move__{move.id}",
                'codigoProducto': codigo_producto,
                'cantidadOperacion': move.product_uom_qty,
                'tipoOperacion': 'M'
            })

        tipo_agente = picking.picking_type_id.tipo_agente_wis or 'PRO'
        _partner_act = picking._get_wis_partner()
        codigo_agente = (_partner_act.codigo_unico_cliente if tipo_agente == 'CLI' else _partner_act.codigo_unico_proveedor) if _partner_act else ''

        payload = {
            'empresa': self.empresa_id,
            'dsReferencia': f"ACTUALIZACIÓN DESDE ODOO: {picking.name}",
            'referencias': [{
                'referencia': picking.codigo_unico,
                # tipoReferencia sale del tipo de operación (Tipo de Pedido WIS); 'OC' si no está.
                'tipoReferencia': picking.picking_type_id.tipo_pedido_wis or 'OC',
                'codigoAgente': codigo_agente,
                'tipoAgente': tipo_agente,
                'predio': '1',
                'fechaEstimada': picking.scheduled_date.isoformat() if picking.scheduled_date else None,
                'detalles': detalles
            }]
        }

        return self.consultarAPI(
            link="/ModificarDetalleReferencia/Update",
            body=payload,
            params=None,
            method="POST"
        )

    def anularReferenciaRecepcion(self, picking):
        """Spec 1.4: anula una referencia de recepción en WIS (cancelación saliente Odoo→WIS).
        Endpoint: POST /AnulacionReferenciaRecepcion/Update. Identifica la operación por su
        `codigo_unico`. Lanza excepción si WIS responde error (la maneja `action_cancel`).
        """
        partner = picking._get_wis_partner()
        tipo_agente = picking.picking_type_id.tipo_agente_wis or 'PRO'
        codigo_agente = ''
        if partner:
            codigo_agente = (partner.codigo_unico_proveedor if tipo_agente == 'PRO'
                             else partner.codigo_unico_cliente) or ''
        body = {
            'empresa': self.empresa_id,
            'dsReferencia': f'Anulación desde Odoo: {picking.name}',
            'referencias': [{
                'referencia': picking.codigo_unico,
                # tipoReferencia sale del tipo de operación (Tipo de Pedido WIS); 'OC' si no está.
                'tipoReferencia': picking.picking_type_id.tipo_pedido_wis or 'OC',
                'codigoAgente': codigo_agente,
                'tipoAgente': tipo_agente,
            }],
        }
        return self.consultarAPI(
            link='/AnulacionReferenciaRecepcion/Update',
            body=body,
            params=None,
            method='POST',
        )

    def consultar(self):
        _logger.info("ESTO ES SOLO UNA PRUEBA");
        return True;
    
    
    
    def editarReferencia(self, vals):
        detalles = [];


        for line in vals.order_line:
            codigo_producto = line.product_id.codigo_unico or ''
            
            detalles.append({
                'idLineaSistemaExterno': f"odoo__purchase.order.line__{line.id}", 
                'codigoProducto': codigo_producto,
                'cantidadOperacion': line.product_qty,
                "tipoOperacion": "M"
            })


        payload = {
            'empresa': self.empresa_id,
            'dsReferencia': "CREACION DE ORDEN DE COMPRA DESDE ODOO", 
            'referencias': [{
                'referencia': vals.referencia,
                'tipoReferencia': 'OC',
                'codigoAgente': vals.partner_id.codigo_unico_proveedor,
                'tipoAgente': 'PRO',
                'predio': "1",
                'detalles': detalles
            }]
        }

        response = self.consultarAPI(
            link="/ModificarDetalleReferencia/Update",
            body=payload,
            params=None,
            method="POST"
        )

        response['referencia'] = vals.name;

        return response
    
    

    def consultaStock(self, vals):

        payload = {
            "empresa": self.empresa_id,
            "codigo": vals['codigo_unico']
        }

        response = self.consultarAPI(
            link="/Producto/GetProducto",
            params=payload,
            method="GET",
            body=None
        )

        return response.get('cantidadGenerica', 0)

    def consultaStockBulk(self, variantes):
        
        if not self.token or self.expiracionToken < datetime.datetime.now():
            self.renovarToken()

        resultado = {}
        for var in variantes:
            if not var.codigo_unico:
                continue
            try:
                payload = {
                    "empresa": self.empresa_id,
                    "codigo": var.codigo_unico,
                }
                response = self.consultarAPI(
                    link="/Producto/GetProducto",
                    params=payload,
                    method="GET",
                    body=None,
                )
                resultado[var.id] = response.get('cantidadGenerica', 0)
            except Exception as e:
                _logger.warning(f"No se pudo obtener stock WIS para {var.display_name}: {str(e)}")
                resultado[var.id] = None
        return resultado

    def sincronizarClientesDesdeWIS(self, clientes, conciliacion_id=None):
        """Consulta cada cliente en WIS y, si los datos difieren, actualiza Odoo.

        WIS es la fuente de verdad. Campos sincronizados WIS → Odoo:
          descripcion → name
          telefonoPrincipal → phone
          direccion → street

        Returns dict con 'actualizados', 'sin_cambios', 'errores', 'errores_detalle'.
        """
        actualizados = 0
        sin_cambios  = 0
        errores      = 0
        errores_detalle = []

        for cliente in clientes:
            if not cliente.codigo_unico:
                continue
            try:
                wis = self.getCliente(cliente.codigo_unico)

                cambios = {}
                detalle_diffs = []

                # descripcion → name
                desc_wis = (wis.get('descripcion') or '').strip()
                if desc_wis and desc_wis != (cliente.name or '').strip():
                    cambios['name'] = desc_wis
                    detalle_diffs.append(f"name: Odoo='{cliente.name}' → WIS='{desc_wis}'")

                # telefonoPrincipal → phone
                tel_wis = (wis.get('telefonoPrincipal') or '').strip()
                if tel_wis and tel_wis != (cliente.phone or '').strip():
                    cambios['phone'] = tel_wis
                    detalle_diffs.append(f"phone: Odoo='{cliente.phone}' → WIS='{tel_wis}'")

                # direccion → street (primer segmento de la dirección)
                dir_wis = (wis.get('direccion') or '').strip()
                if dir_wis and dir_wis != (cliente.street or '').strip():
                    cambios['street'] = dir_wis
                    detalle_diffs.append(f"street: Odoo='{cliente.street}' → WIS='{dir_wis}'")

                if cambios:
                    cliente.with_context(no_reindex=True, skip_wis_sync=True).write(cambios)
                    cliente.message_post(
                        body=f"Cliente actualizado desde WIS. Cambios: {', '.join(detalle_diffs)}"
                    )
                    actualizados += 1
                    _logger.info("[WIS] sincronizarClientesDesdeWIS | %s actualizado | %s",
                                 cliente.name, ', '.join(detalle_diffs))
                    if conciliacion_id:
                        self.env['logs.conciliacion'].create({
                            'conciliacion_id': conciliacion_id,
                            'texto': f"{cliente.name} actualizado en Odoo desde WIS: {', '.join(detalle_diffs)}",
                            'modelo': 'res.partner',
                            'nivel': 'info',
                            'fecha': fields.Datetime.now(),
                        })
                else:
                    sin_cambios += 1

            except Exception as e:
                errores += 1
                errores_detalle.append(f"{cliente.name}: {str(e)}")
                _logger.error("[WIS] sincronizarClientesDesdeWIS | error en %s: %s",
                              cliente.name, str(e))
                if conciliacion_id:
                    self.env['logs.conciliacion'].create({
                        'conciliacion_id': conciliacion_id,
                        'texto': f"Error al sincronizar cliente {cliente.name} desde WIS: {str(e)}",
                        'modelo': 'res.partner',
                        'nivel': 'error',
                        'fecha': fields.Datetime.now(),
                    })

        _logger.info(
            "[WIS] sincronizarClientesDesdeWIS | FINALIZADO | actualizados=%d | sin_cambios=%d | errores=%d",
            actualizados, sin_cambios, errores
        )
        return {
            'actualizados':    actualizados,
            'sin_cambios':     sin_cambios,
            'errores':         errores,
            'errores_detalle': errores_detalle,
        }

    def sincronizarProductosDesdeWIS(self, variantes, conciliacion_id=None):
        """Consulta cada producto en WIS y, si los datos difieren, actualiza Odoo.

        WIS es la fuente de verdad. Odoo se actualiza para reflejar lo que WIS tiene.

        Returns dict con 'actualizados', 'sin_cambios', 'errores', 'errores_detalle'.
        """
        actualizados = 0
        sin_cambios = 0
        errores = 0
        errores_detalle = []

        # Campos que se sincronizan WIS → Odoo
        # clave WIS : (campo Odoo, función de conversión opcional)
        CAMPOS_SYNC = {
            'descripcion': 'name',
            'pesoNeto':    'weight',
            'precioVenta': 'list_price',
            'activo':      'active',
        }

        for variant in variantes:
            if not variant.codigo_unico:
                continue
            try:
                wis = self.getProducto(variant.codigo_unico)

                cambios = {}
                detalle_diffs = []

                for campo_wis, campo_odoo in CAMPOS_SYNC.items():
                    val_wis  = wis.get(campo_wis)
                    val_odoo = getattr(variant, campo_odoo)

                    # Normalizar nombre: WIS puede devolverlo en mayúsculas
                    if campo_wis == 'descripcion' and val_wis:
                        val_wis = val_wis.strip()

                    if val_wis is not None and val_wis != val_odoo:
                        cambios[campo_odoo] = val_wis
                        detalle_diffs.append(
                            f"{campo_wis}: Odoo='{val_odoo}' → WIS='{val_wis}'"
                        )

                # Categoría: buscar por nombre en Odoo
                cat_wis = wis.get('categoria1', '').strip() if wis.get('categoria1') else ''
                if cat_wis and variant.categ_id.name != cat_wis:
                    categoria = self.env['product.category'].search(
                        [('name', '=', cat_wis)], limit=1
                    )
                    if categoria:
                        cambios['categ_id'] = categoria.id
                        detalle_diffs.append(
                            f"categoria1: Odoo='{variant.categ_id.name}' → WIS='{cat_wis}'"
                        )

                if cambios:
                    variant.with_context(_avoid_wms=True).write(cambios)
                    variant.message_post(
                        body=f"Producto actualizado desde WIS. Cambios: {', '.join(detalle_diffs)}"
                    )
                    actualizados += 1
                    _logger.info("[WIS] sincronizarDesdeWIS | %s actualizado | %s",
                                 variant.display_name, ', '.join(detalle_diffs))

                    if conciliacion_id:
                        self.env['logs.conciliacion'].create({
                            'conciliacion_id': conciliacion_id,
                            'texto': f"{variant.name} actualizado en Odoo desde WIS: {', '.join(detalle_diffs)}",
                            'modelo': 'product.product',
                            'nivel': 'info',
                            'fecha': fields.Datetime.now(),
                        })
                else:
                    sin_cambios += 1

            except Exception as e:
                errores += 1
                errores_detalle.append(f"{variant.display_name}: {str(e)}")
                _logger.error("[WIS] sincronizarDesdeWIS | error en %s: %s",
                              variant.display_name, str(e))
                if conciliacion_id:
                    self.env['logs.conciliacion'].create({
                        'conciliacion_id': conciliacion_id,
                        'texto': f"Error al sincronizar {variant.name} desde WIS: {str(e)}",
                        'modelo': 'product.product',
                        'nivel': 'error',
                        'fecha': fields.Datetime.now(),
                    })

        _logger.info(
            "[WIS] sincronizarDesdeWIS | FINALIZADO | actualizados=%d | sin_cambios=%d | errores=%d",
            actualizados, sin_cambios, errores
        )
        return {
            'actualizados': actualizados,
            'sin_cambios':  sin_cambios,
            'errores':      errores,
            'errores_detalle': errores_detalle,
        }

    def getProducto(self, codigo):
        payload = {
            "empresa": self.empresa_id,
            "codigo": codigo
        }

        response = self.consultarAPI(
            link="/Producto/GetProducto",
            params=payload,
            method="GET",
            body=None
        )

        return response

    def getCliente(self, vat):
        payload = {
            "empresa": self.empresa_id,
            "codigo": vat,
            "tipo": "CLI"
        }

        response = self.consultarAPI(
            link="/Agente/GetAgente",
            params=payload,
            method="GET",
            body="None"
        )

        return response

    def getAgente(self, codigo, tipo):
        """Consulta un agente en WIS por código y tipo ('CLI' o 'PRO')."""
        payload = {
            "empresa": self.empresa_id,
            "codigo": codigo,
            "tipo": tipo,
        }
        return self.consultarAPI(
            link="/Agente/GetAgente",
            params=payload,
            method="GET",
            body=None,
        )

    def conciliarClientes(self, clientes, conciliacion_id=None):
        cantidad = 0;

        for vals in clientes:
            try:
                vat_clean = vals.codigo_unico;
                name_clean = (vals.name or "").upper()
                street_clean = (vals.street or "").upper()
                city_clean = (vals.city or "").upper()
                phone_clean = (vals.phone or "").replace(" ", "").replace("-", "");

                country_code = "UY"  
                if vals.country_id and vals.country_id.code:
                    country_code = vals.country_id.code.upper()

                punto_entrega_parts = []
            
                if vals.street:
                    punto_entrega_parts.append(f"Calle: {vals.street}")
                if vals.street2:
                    punto_entrega_parts.append(f"Esquina: {vals.street2}")
                
                if vals.city:
                    ciudad_codigo = vals.city
                    if vals.zip:
                        ciudad_codigo += f" (CP: {vals.zip})"
                    punto_entrega_parts.append(ciudad_codigo)
                elif vals.zip:
                    punto_entrega_parts.append(f"CP: {vals.zip}")
                
                if vals.state_id and vals.state_id.name:
                    punto_entrega_parts.append(f"Provincia: {vals.state_id.name}")
                
                if hasattr(vals, 'barrio') and vals.barrio:
                    punto_entrega_parts.append(f"Barrio: {vals.barrio}")
                elif hasattr(vals, 'l10n_uy_barrio') and vals.l10n_uy_barrio:
                    punto_entrega_parts.append(f"Barrio: {vals.l10n_uy_barrio}")
                
                if hasattr(vals, 'ref') and vals.ref:
                    punto_entrega_parts.append(f"Ref: {vals.ref}")
                
                punto_entrega = " - ".join(punto_entrega_parts) if punto_entrega_parts else "Sin dirección especificada"
                
                punto_entrega = punto_entrega[:200] 

                try:

                    response = self.getCliente(vat_clean);

                except Exception as e:
                    _logger.info("No se encontró el cliente en WIS, se intentará crear uno nuevo. Error: %s", str(e))
                    self.insertarClienteOrSupplier(vals, 'CLI');
                    continue

                if response.get('codigoAgente') != f"CLI-{vat_clean}" or response.get('descripcion') != name_clean or response.get('direccion') != punto_entrega or response.get('telefonoPrincipal') != phone_clean:
                    if conciliacion_id:
                        self.env['logs.conciliacion'].create({
                            'texto': f"El cliente/proveedor {vals.name} será actualizado en WIS.",
                            'modelo': 'res.partner',
                            'fecha': fields.Datetime.now(),
                            'conciliacion_id': conciliacion_id
                        })

                        if response.get('codigoAgente') != f"CLI-{vat_clean}":
                            self.env['logs.conciliacion'].create({
                                'texto': f"Actualizando código de {vals.name} de {response.get('codigoAgente')} a CLI-{vat_clean}",
                                'modelo': 'res.partner',
                                'fecha': fields.Datetime.now(),
                                'conciliacion_id': conciliacion_id
                            })

                        if response.get('descripcion') != name_clean:
                            self.env['logs.conciliacion'].create({
                                'texto': f"Actualizando nombre de {vals.name} de '{response.get('descripcion')}' a '{name_clean}'",
                                'modelo': 'res.partner',
                                'fecha': fields.Datetime.now(),
                                'conciliacion_id': conciliacion_id
                            })

                        if response.get('direccion') != punto_entrega:
                            self.env['logs.conciliacion'].create({
                                'texto': f"Actualizando dirección de {vals.name} de '{response.get('direccion')}' a '{punto_entrega}'",
                                'modelo': 'res.partner',
                                'fecha': fields.Datetime.now(),
                                'conciliacion_id': conciliacion_id
                            })
                        
                        


                    response = self.insertarClienteOrSupplier(vals, 'CLI');

                    if conciliacion_id:
                        self.env['logs.conciliacion'].create({
                            'texto': f"Respuesta de la API para {vals.name}: {response}",
                            'modelo': 'res.partner',
                            'fecha': fields.Datetime.now(),
                            'conciliacion_id': conciliacion_id
                        })
                    vals.message_post(body=f"El cliente/proveedor {vals.name} será actualizado en WIS.")
                



                if conciliacion_id:
                    self.env['logs.conciliacion'].create({
                        'texto': f"Respuesta de la API para {vals.name}: {response}",
                        'modelo': 'res.partner',
                        'fecha': fields.Datetime.now(),
                        'conciliacion_id': conciliacion_id
                    })

                cantidad += 1;
                vals.message_post(body=f"Se insertó/actualizó el cliente en WIS. Respuesta de la API: {response}")
            except Exception as e:
                self.env['logs.conciliacion'].create({
                    'texto': f"Error al procesar {vals.name}: {str(e)}",
                    'modelo': 'res.partner',
                    'nivel': 'error',
                    'fecha': fields.Datetime.now(),
                    'conciliacion_id': conciliacion_id
                })


        self.env['logs.conciliacion'].create({
            'texto': f"Se insertaron/actualizaron: {cantidad} clientes/proveedores",
            'modelo': 'res.partner',
            'fecha': fields.Datetime.now(),
            'conciliacion_id': self.id
        })
        return;

    

    def consultarProductos(self, variantes, conciliacion_id=None):
        cantidad = 0
        cantidadErrores = 0;

        for vals in variantes:
            try:
                response = self.getProducto(vals.codigo_unico)

                if conciliacion_id:
                    self.env['logs.conciliacion'].create({
                        'texto': f"Consultando producto {vals.name} en WIS...",
                        'modelo': 'product.product',
                        'fecha': fields.Datetime.now(),
                        'conciliacion_id': conciliacion_id
                    })

                nombre_odoo = vals.name[:65] if len(vals.name) > 65 else vals.name
                categoria_odoo = vals.categ_id.name if vals.categ_id else ""

                comparaciones = {
                    'descripcion':      (response.get('descripcion'),     nombre_odoo),
                    'precioVenta':    (response.get('precioVenta'),    vals.list_price),
                    
                    'pesoNeto':         (response.get('pesoNeto'),         vals.weight),
                    'categoria1':       (response.get('categoria1'),       categoria_odoo),
                    'activo':           (response.get('activo'),           vals.active),
                }

                diferencias = {
                    campo: {'wis': val_wis, 'odoo': val_odoo}
                    for campo, (val_wis, val_odoo) in comparaciones.items()
                    if val_wis != val_odoo
                }

                if diferencias:
                    detalle_diffs = ", ".join(
                        f"{campo}: WIS='{d['wis']}' → Odoo='{d['odoo']}'"
                        for campo, d in diferencias.items()
                    )

                    if conciliacion_id:
                        self.env['logs.conciliacion'].create({
                            'texto': f"Diferencias encontradas en {vals.name}: {detalle_diffs}",
                            'modelo': 'product.product',
                            'fecha': fields.Datetime.now(),
                            'conciliacion_id': conciliacion_id
                        })

                    try:
                        resAPI = self.insertarProducto(vals)

                        if conciliacion_id:
                            self.env['logs.conciliacion'].create({
                                'texto': f"Producto {vals.name} actualizado en WIS. Respuesta: {resAPI}",
                                'modelo': 'product.product',
                                'fecha': fields.Datetime.now(),
                                'conciliacion_id': conciliacion_id
                            })

                        vals.message_post(body=f"Producto actualizado en WIS. Cambios: {detalle_diffs}")
                        cantidad += 1

                    except Exception as update_err:
                        err_str = str(update_err)
                        if 'ManejoIdentificador' in err_str and 'No se permite modificar' in err_str:
                            # El producto ya existe en WIS con tracking asignado.
                            # Este campo no se puede cambiar una vez que hay movimientos.
                            # Se omite sin contar como error.
                            if conciliacion_id:
                                self.env['logs.conciliacion'].create({
                                    'texto': f"Producto {vals.name} omitido: manejoIdentificador bloqueado en WIS (producto en uso). Sin impacto.",
                                    'modelo': 'product.product',
                                    'nivel': 'warning',
                                    'fecha': fields.Datetime.now(),
                                    'conciliacion_id': conciliacion_id
                                })
                        else:
                            raise

                else:
                    if conciliacion_id:
                        self.env['logs.conciliacion'].create({
                            'texto': f"Producto {vals.name} está sincronizado con WIS, sin diferencias.",
                            'modelo': 'product.product',
                            'fecha': fields.Datetime.now(),
                            'conciliacion_id': conciliacion_id
                        })

            except Exception as e:
                self.env['logs.conciliacion'].create({
                    'texto': f"Error al procesar {vals.name}: {str(e)}",
                    'modelo': 'product.product',
                    'nivel': 'error',
                    'fecha': fields.Datetime.now(),
                    'conciliacion_id': conciliacion_id
                })
                cantidadErrores += 1

        _logger.info("Conciliación finalizada. Productos actualizados: %s, Errores: %s", cantidad, cantidadErrores)

        self.env['logs.conciliacion'].create({
            'texto': f"Conciliación finalizada. Productos actualizados: {cantidad}",
            'modelo': 'product.product',
            'fecha': fields.Datetime.now(),
            'conciliacion_id': conciliacion_id
        })

        if conciliacion_id:
            conciliacion = self.env['conciliacion.maestros'].browse(conciliacion_id)
            if cantidadErrores == 0:
                conciliacion.estado = 'completado'
            else:
                conciliacion.estado = 'completado_con_errores'
        return


    def conciliarStockProducto(self, variant, conciliacion_id=None, cantidad_wis=None):

        if not self.ubicacionesAConsultar:
            if conciliacion_id:
                self.env['logs.conciliacion.stock'].create({
                    'texto': 'No hay ubicaciones configuradas para consultar stock en la integración WIS.',
                    'nivel': 'warning',
                    'conciliacion_id': conciliacion_id
                })
            return

        if not variant.codigo_unico:
            if conciliacion_id:
                self.env['logs.conciliacion.stock'].create({
                    'texto': f'El producto {variant.display_name} no tiene código único en WIS. Se omite.',
                    'nivel': 'warning',
                    'conciliacion_id': conciliacion_id
                })
            return

        quants = self.env['stock.quant'].search([
            ('product_id', '=', variant.id),
            ('location_id', 'in', self.ubicacionesAConsultar.ids)
        ])
        cantidad_odoo = sum(quants.mapped('quantity'))

        if cantidad_wis is None:
            cantidad_wis = self.consultaStock(variant)

        diferencia = abs(cantidad_odoo - cantidad_wis)

        if diferencia >= self.diferenciaMinima:
            if conciliacion_id:
                self.env['logs.conciliacion.stock'].create({
                    'texto': (
                        f'Diferencia de stock en {variant.display_name}: '
                        f'Odoo={cantidad_odoo} | WIS={cantidad_wis} | Diferencia={diferencia}'
                    ),
                    'nivel': 'warning',
                    'conciliacion_id': conciliacion_id
                })


                if not self.ubicacionReponerStock:
                    if conciliacion_id:
                        self.env['logs.conciliacion.stock'].create({
                            'texto': (
                                f'No se puede ajustar stock de {variant.display_name}: '
                                f'no hay ubicación de reposición configurada.'
                            ),
                            'nivel': 'error',
                            'conciliacion_id': conciliacion_id
                        })
                    return

                quant_ajuste = self.env['stock.quant'].search([
                    ('product_id', '=', variant.id),
                    ('location_id', '=', self.ubicacionReponerStock.id),
                ], limit=1)

                if quant_ajuste:
                    quant_ajuste.write({
                        'inventory_quantity': cantidad_wis,
                    })
                else:
                    self.env['stock.quant'].create({
                        'product_id': variant.id,
                        'location_id': self.ubicacionReponerStock.id,
                        'inventory_quantity': cantidad_wis
                    })
        else:
            if conciliacion_id:
                self.env['logs.conciliacion.stock'].create({
                    'texto': (
                        f'Stock de {variant.display_name} dentro del margen. '
                        f'Odoo={cantidad_odoo} | WIS={cantidad_wis}'
                    ),
                    'nivel': 'info',
                    'conciliacion_id': conciliacion_id
                })

    def consultarCodigosBarras(self, variantes, conciliacion_id):

        
        self.env['logs.conciliacion'].create({
            'texto': f"Comenzando la consulta de códigos de barra en WIS para {len(variantes)} productos",
            'modelo': 'product.product',
            'fecha': fields.Datetime.now(),
            'conciliacion_id': conciliacion_id
        })

        for vals in variantes:
            try:

                if not vals.barcode:
                    continue;

                codigosBarras = vals.barcode.split(',');

                for cod in codigosBarras:
                    response = self.existeBarcode(cod);

                    

                    if response == False:
                        self.env['logs.conciliacion'].create({
                            'texto': f"El código de barra {cod} no existe en WIS para el producto {vals.name}, se procederá a insertarlo.",
                            'modelo': 'product.product',
                            'fecha': fields.Datetime.now(),
                            'conciliacion_id': conciliacion_id
                        })

                        insertar = self.insertarBarcode(vals);
                    else:
                        self.env['logs.conciliacion'].create({
                            'texto': f"El código de barra {cod} ya existe en WIS para el producto {vals.name}, no se realizará ninguna acción.",
                            'modelo': 'product.product',
                            'fecha': fields.Datetime.now(),
                            'conciliacion_id': conciliacion_id
                        })

                    cod.message_post(body=f"Se procesó el código de barra {cod}. Respuesta de la API: {insertar if response == False else 'El código ya existía, no se insertó.'}")



                pass
            except Exception as e:
                self.env['logs.conciliacion'].create({
                    'texto': f"Error al procesar {vals.name}: {str(e)}",
                    'modelo': 'product.product',
                    'nivel': 'error',
                    'fecha': fields.Datetime.now(),
                    'conciliacion_id': conciliacion_id
                })




class IntegracionWISWebHooks(models.Model):
    _name = "integracion_wis.integracion_wis_webhook"
    _description = "Configuración de Webhooks para la integración con WIS"

    company_id = fields.Many2one(
        'res.company',
        string='Compañía',
        required=True,
        ondelete='cascade',
        help='Compañía para la cual se configura el Webhook'
    )

    claveSecreta = fields.Char(
        string='Clave Secreta',
        help="Clave secreta para autenticar las solicitudes del Webhook",
        required=True
    )

