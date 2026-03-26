from odoo import models, fields, api
from odoo.exceptions import UserError, ValidationError
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

    partner = fields.Many2one('res.partner', string='Partner por defecto', help='Partner asignado a los movimientos de stock generados', required=False)
    

    _sql_constraints = [
        ('company_unique', 'unique(company_id)', '¡Solo puede existir una configuración por compañía!'),
    ]

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
            req = requests.request(url=api_url + link, method=method, json=body, headers=headers, params=params)
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


    def insertarProducto(self, vals):
        

        productos = [];
        unidad_wis = vals.uom_id.name if vals.uom_id else "UND"
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
        productos = [{
                "codigoProducto": codigo,
                "codigo": codigo,
                "descripcion": f"{vals.name}",
                "familia": 1,
                "unidadMedida": "L",
                "clase": 1,
                "ramo": 1,
                "precioIngreso": vals.standard_price,
                "pesoNeto": vals.weight,
                "manejoIdentificador": "L",
                "tipoManejoFecha": "F",
                "precioVenta": vals.list_price,

                "categoria1": vals.categ_id.name if vals.categ_id else "",
                "unidadBulto": 1,
                "activo": vals.active

            }]
        

        


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
        #         {
        # "empresa": 0,
        # "dsReferencia": "string",
        # "archivo": "string",
        # "idRequest": "123",
        # "transferencias": [
        #     {
        #     "ubicacion": "string",
        #     "ubicacionDestino": "string",
        #     "codigoProducto": "string",
        #     "identificador": "string",
        #     "cantidad": 0
        #     }
        # ]
        # }

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





    def insertarClienteOrSupplier(self, vals):

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

        codigoAgente = '';
        if vals.codigo_unico:
            codigoAgente = vals.codigo_unico
        else:
            codigoAgente = f"CLI-{numeroRandom}" if vals.customer_rank > 0 else f"PRO-{numeroRandom}"


        agentes = [{
            "codigoAgente": codigoAgente,
            "tipo": "CLI" if vals.customer_rank > 0 else "PRO",
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
            "tipoAgente": "CLI" if vals.partner_id.customer_rank > 0 else "PRO",
            "codigoAgente": vals.partner_id.codigo_unico,
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
            exception = req.json().get('detail', 'Ocurrió un error')
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

        hash_obj = hashlib.md5(vals.name.encode())
        hash_short = hash_obj.hexdigest()[:8].upper()
        detalles = [];
        for prod in vals.move_ids:
            detalles.append({
                "codigoProducto": "PRD-" + str(prod.product_id.id),
                "identificador": prod.product_id.codigo_unico,
                "cantidad": prod.product_uom_qty
            })

        _logger.info(f"TERMINANDO DE ASIGNAR DETALLES detalles: {detalles}")

        direccion = f'{vals.location_dest_id.name}';

        if vals.picking_type_id.code and vals.location_id and vals.location_dest_id:
            direccion = f'ORIGEN: {vals.location_id.name} - DESTINO {vals.location_dest_id.name}';
            #self.transferirStock(vals);

        # Determinar si es crossdocking
        


        
        direccion = vals.location_dest_id.name;


        pedidos = [{
            "nroPedido": vals.codigo_unico if vals.codigo_unico else f"P{hash_short}",
            "codigoAgente": vals.partner_id.codigo_unico,
            "tipoAgente": "CLI" if vals.partner_id.customer_rank > 0 else "PRO",
            "fechaEntrega": vals.scheduled_date.isoformat(),
            "tipoPedido": tipo,
            "direccion": direccion,
            "detalles": detalles
        }]




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

        response['codigoUnico'] = vals.codigo_unico if vals.codigo_unico else f"P{hash_short}";
        

        _logger.info(f"RESPONSE => {response}")
        return response;


    def insertarDevolucion(self, picking):
            moves = picking.move_ids or (hasattr(picking, 'move_ids_without_package') and picking.move_ids_without_package) or []
            
            if not moves:
                _logger.info("No hay productos detectados en la devolución %s", picking.name)
                return False
                
            detalles = []
            for move in moves:
                codigo_producto = move.product_id.codigo_unico or ''
                detalles.append({
                    'idLineaSistemaExterno': f"odoo__stock.move__{move.id}",
                    'codigoProducto': codigo_producto,
                    'cantidadReferencia': move.product_uom_qty
                })

            numeroRandom = random.randint(100000, 999999);
            payload = {
                'empresa': self.empresa_id,
                'dsReferencia': f"DEVOLUCIÓN DE CLIENTE DESDE ODOO: {picking.name}",
                'referencias': [{
                    'referencia': 'DEV-' + str(numeroRandom),
                    'tipoReferencia': 'OD',  # Order Delivery Return
                    'codigoAgente': picking.partner_id.codigo_unico or picking.partner_id.vat,
                    'tipoAgente': 'CLI',
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

            response['codigoUnico'] = f'DEV-{numeroRandom}'
            return response

    def insertarReferenciaRecepcion(self, picking):

        _logger.info("PICKINGS => %s", picking);
        
        _logger.info("CAMPOS DEL PICKING: %s", picking._fields.keys())
        _logger.info("VALORES CARGADOS: %s", picking.read())

        moves = picking.move_ids or (hasattr(picking, 'move_ids_without_package') and picking.move_ids_without_package) or []
        
        if not moves:
            _logger.info("No hay productos detectados en el movimiento %s.", picking.name)
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
            
        detalles = []
        for move in moves:
            codigo_producto = move.product_id.codigo_unico or ''
            
            if not codigo_producto:
                raise ValidationError(f"No se encontró código único WIS en el producto: {move.product_id.name}. Por favor, asegúrese de que el producto esté sincronizado con WIS antes de enviar el movimiento.")
                
            detalles.append({
                'idLineaSistemaExterno': f"odoo__stock.move__{move.id}",
                'codigoProducto': codigo_producto,
                'cantidadReferencia': move.product_uom_qty
            })

        numeroRandom = random.randint(100000, 999999)
        payload = {
            'empresa': self.empresa_id,
            'dsReferencia': f"RECEPCIÓN DESDE ODOO: {picking.name}",
            'referencias': [{
                'referencia': 'REC-' + str(numeroRandom),
                'tipoReferencia': 'OC',
                'codigoAgente': picking.partner_id.codigo_unico,
                'tipoAgente': 'PRO' if picking.partner_id.supplier_rank > 0 or not picking.partner_id.customer_rank > 0 else 'CLI',
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

        response['codigoUnico'] = f'REC-{numeroRandom}'

        return response

    def consultar(self):
        _logger.info("ESTO ES SOLO UNA PRUEBA");
        return True; 
    
    def insertarOrdenDeCompra(self, vals):
        detalles = [];

        _logger.info(f"VALS => {vals}");
        _logger.info(f"SELF => {self}");
        _logger.info(f"VALS DICT => {vals.order_line}");


        for line in vals.order_line:
            _logger.info("ENTRA ACA");
            codigo_producto = line.product_id.codigo_unico or ''
            
            detalles.append({
                'idLineaSistemaExterno': f"odoo__purchase.order.line__{line.id}", 
                'codigoProducto': codigo_producto,
                'cantidadReferencia': line.product_qty
            })


        _logger.info(f"DATOS EN DETALLES => {detalles}");

        numeroRandom = random.randint(100000, 999999);

        payload = {
            'empresa': self.empresa_id,
            'dsReferencia': "CREACION DE ORDEN DE COMPRA DESDE ODOO", 
            'referencias': [{
                'referencia': numeroRandom,
                'tipoReferencia': 'OC',
                'codigoAgente': vals.partner_id.codigo_unico,
                'tipoAgente': 'CLI' if vals.partner_id.customer_rank > 0 else 'PRO' ,
                'predio': "1",
                'detalles': detalles
            }]
        }

        response = self.consultarAPI(
            link="/ReferenciaRecepcion/Create",
            body=payload,
            params=None,
            method="POST"
        )

        response['referencia'] = numeroRandom;

        return response
    
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
                'codigoAgente': vals.partner_id.codigo_unico,
                'tipoAgente': 'CLI' if vals.partner_id.customer_rank > 0 else 'PRO' ,
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
        """Consulta el stock de WIS para una lista de variantes de una sola vez.
        Renueva el token una única vez antes del loop para no repetir la verificación
        en cada llamada individual.
        Retorna un dict {variant.id: cantidad_wis}.
        Nota: la API WIS no expone un endpoint bulk; se consulta artículo por artículo
        pero con un único token activo y sin overhead de renovación repetida.
        """
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

        return response;

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



                    self.insertarClienteOrSupplier(vals);


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
                        
                        


                    response = self.insertarClienteOrSupplier(vals);

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

                # Construir los valores esperados en WIS (igual que insertarProducto)
                nombre_odoo = vals.name[:65] if len(vals.name) > 65 else vals.name
                categoria_odoo = vals.categ_id.name if vals.categ_id else ""

                # Mapeo de campos Odoo → WIS para comparar
                comparaciones = {
                    'descripcion':      (response.get('descripcion'),     nombre_odoo),
                    'precioIngreso':    (response.get('precioIngreso'),    vals.standard_price),
                    'precioVenta':      (response.get('precioVenta'),      vals.list_price),
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

        self.env['logs.conciliacion'].create({
            'texto': f"Conciliación finalizada. Productos actualizados: {cantidad}",
            'modelo': 'product.product',
            'fecha': fields.Datetime.now(),
            'conciliacion_id': conciliacion_id
        })
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

        # Sumar cantidades en las ubicaciones configuradas
        quants = self.env['stock.quant'].search([
            ('product_id', '=', variant.id),
            ('location_id', 'in', self.ubicacionesAConsultar.ids)
        ])
        cantidad_odoo = sum(quants.mapped('quantity'))

        # Usar cantidad WIS preconsultada si está disponible, o consultar ahora
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
                    quant_ajuste.with_context(inventory_mode=True).write({
                        'inventory_quantity': cantidad_wis,
                    })
                else:
                    self.env['stock.quant'].with_context(inventory_mode=True).create({
                        'product_id': variant.id,
                        'location_id': self.ubicacionReponerStock.id,
                        'inventory_quantity': cantidad_wis,
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

                for cod in codigoBarras:
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

