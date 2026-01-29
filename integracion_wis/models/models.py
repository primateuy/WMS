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

    ubicacionSalida = fields.Many2one(
        'stock.location',
        string='Ubicación de Salida',
        domain=[('usage', '=', 'internal')],
        help='Ubicación predeterminada de salida para conciliaciones de stock.'
    )
    ubicacionDestino = fields.Many2one(
        'stock.location',
        string='Ubicación de Destino',
        domain=[('usage', '=', 'internal')],
        help='Ubicación predeterminada de destino para conciliaciones de stock.'
    )
    diferenciaMinima = fields.Integer(
        string='Diferencia Mínima',
        default=1,
        help='Diferencia mínima para registrar un ajuste de stock.'
    )

    partner = fields.Many2one('res.partner', string='Partner por defecto', help='Partner asignado a los movimientos de stock generados', required=False)
    

    _sql_constraints = [
        ('company_unique', 'unique(company_id)', '¡Solo puede existir una configuración por compañía!'),
    ]

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
        _logger.info("ENTRANDO A CONSULTAR API")

        if not self.apiLink or not self.client_id or not self.client_secret or not self.url_access_token:
            raise ValidationError("Faltan datos para acceder a la API")
                
        if not self.token or self.expiracionToken < datetime.datetime.now():
            self.renovarToken()

        headers = {
            "Content-Type": "application/json",
            "accept-language": "es",
            "Authorization": f"Bearer {self.token}" 
        }

        _logger.info("Llega hasta los headers")

        try:
            req = requests.request(url=self.apiLink + link, method=method, json=body, headers=headers, params=params)
            _logger.info(f"REQ: {req.text}")

            # Verificar el código de estado de la respuesta
            if req.status_code != 200:
                _logger.error(f"Error en la API: {req.status_code} - {req.text}")
                raise ValidationError(f"Error en la API: {req.status_code} - {req.text}")

            # Intentar decodificar la respuesta como JSON
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
            "empresa": 6005,
            "productos": [{
                    "codigoProducto": vals.codigo_unico,
                    "cantidadGenerica": vals.qty_available 
            }]
        }

        response = self.consultarAPI(
            link="/Producto/CreateOrUpdate",
            params=payload,
            method="POST",
            body=None
        )

        if response.get('cantidadGenerica') is None:
            raise ValidationError("No se encontró el producto en WIS")

        vals.qty_available = response['cantidadGenerica']
        vals.with_context(_avoid_wms=True).write({'qty_available': vals.qty_available})

        _logger.info(f"El usuario: {self.company_id.id} actualizó el stock del producto {vals.name} - {vals.id} - {vals.codigo_unico} a {vals.qty_available}, en el día {datetime.datetime.now()}");
        return True;


    def insertarProducto(self, vals):
        

        productos = [];
        unidad_wis = vals.uom_id.name if vals.uom_id else "UND"
        numeroRandom = random.randint(100000, 999999);
        codigo = ''
        if vals.codigo_unico:
            codigo = vals.codigo_unico;
        else:
            codigo = f"PRD-{numeroRandom}"
        productos = [{
                "codigoProducto": codigo,
                "codigo": codigo,
                "descripcion": f"{vals.display_name}",
                "familia": 1,
                "unidadMedida": "L",
                "clase": 1,
                "ramo": 1,
                "manejoIdentificador": "L",
                "tipoManejoFecha": "F",
                "unidadBulto": 1,
                "activo": vals.active,
                'cantidadGenerica': vals.qty_available or 1

            }]
        

        


        payload = {
            "empresa": 6005,
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
            "empresa": 6005,
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
            "empresa": 6005,
            "codigo": int(vals.barcode)
        }



        _logger.info("Realizando consulta de existencia de código de barras en WIS %s", params);


        req = requests.get(
            url=f"{self.apiLink}/CodigoBarras/GetCodigoBarras",
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
            "empresa": 6005,
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
            "empresa": 6005,
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
            "empresa": 6005,
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

            self.env['logs.res.partner'].create({
                'partner_id': vals.id,
                'fecha': fields.Datetime.now(),
                'texto': 'Operación finalizada correctamente, se creó/actualizó el agente en WIS: ' + str(response),
            })

        except Exception as e:
            self.env['logs.res.partner'].create({
                'partner_id': vals.id,
                'fecha': fields.Datetime.now(),
                'texto': f'Error al enviar la información a WIS: {str(e)}',
            })
            raise ValidationError(f"Error al enviar la información a WIS: {str(e)}")

        

        return response

    def getPedido(self, vals):

        if not vals:
            raise ValidationError("No se ha encontrado información del pedido");

        payload = {
            "empresa": 6005,
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


   

    def insertarPedidos(self, vals, tipo, pdfData):
        tipoPedido = 'VEN';

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
            "tipoPedido": tipoPedido,
            "direccion": direccion,
            "detalles": detalles
        }]




        payload = {
            "empresa": 6005,
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
            """
            Inserta devolución de cliente en WMS
            Args:
                picking: stock.picking object (devolución de cliente)
            """
            _logger.info("🔄 ENTRANDO A INSERTAR DEVOLUCIÓN para picking: %s", picking.name)

            detalles = []
            
            if not picking.move_ids:
                raise ValidationError("No hay productos en la devolución")
                
            for move in picking.move_ids:
                _logger.info("📦 Procesando producto: %s - Cantidad: %s", move.product_id.name, move.product_uom_qty)
                
                # Usar el código que tenga el producto
                codigo_producto = move.product_id.codigo_unico or ''
                
                detalles.append({
                    'idLineaSistemaExterno': f"odoo__stock.move__{move.id}",
                    'codigoProducto': codigo_producto,
                    'cantidadReferencia': move.product_uom_qty
                })

            _logger.info(f"📋 DATOS EN DETALLES DEVOLUCIÓN => {detalles}")

            # Determinar ubicación de destino
            ubicacion_destino = "1"  # Default
            if picking.location_dest_id:
                ubicacion_destino = picking.location_dest_id.name


            numeroRandom = random.randint(100000, 999999);
            payload = {
                'empresa': 6005,
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
            'empresa': 6005,
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
        _logger.info(f"RESPONSE {response}")

        return response
    
    def editarReferencia(self, vals):
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
                'cantidadOperacion': line.product_qty,
                "tipoOperacion": "M"
            })


        _logger.info(f"DATOS EN DETALLES => {detalles}");

        payload = {
            'empresa': 6005,
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
        _logger.info(f"RESPONSE {response}")
        _logger.info("EDITADO");

        return response
    
    

    def consultaStock(self, vals):

        payload = {
            "empresa": 6005,
            "codigo": vals['codigo_unico']
        }

        response = self.consultarAPI(
            link="/Producto/GetProducto",
            params=payload,
            method="GET",
            body=None
        )

        _logger.info(f"RESPUESTA => {response}")

        _logger.info(f"El usuario: {self.company_id.id} realizó una consulta de stock para el producto {vals.name} - {vals.id} - {vals.codigo_unico}, en el día {datetime.datetime.now()}");
        return response.get('cantidadGenerica', 0);

    def getProducto(self, codigo):
        payload = {
            "empresa": 6005,
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
            "empresa": 6005,
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

                response = self.getCliente(vat_clean);

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

        cantidad = 0;

        for vals in variantes:
            try:
                response = self.getProducto(vals.codigo_unico)

                if conciliacion_id:
                    self.env['logs.conciliacion'].create({
                        'texto': f"Consulta de producto {vals.name}: {response}",
                        'modelo': 'product.product',
                        'fecha': fields.Datetime.now(),
                        'conciliacion_id': conciliacion_id
                })
                
                if response.get('cantidadGenerica') != vals.qty_available or response.get('activo') != vals.active or response.get('descripcion'):

                    if response.get('cantidadGenerica') != vals.qty_available:
                        
                        
                        if conciliacion_id:
                            self.env['logs.conciliacion'].create({
                                'texto': f"Actualizando stock de {vals.name} de {response.get('cantidadGenerica')} a {vals.qty_available}",
                                'modelo': 'product.product',
                                'fecha': fields.Datetime.now(),
                                'conciliacion_id': conciliacion_id
                            })
                    
                    if response.get('descripcion') != vals.display_name:
                        
                        if conciliacion_id:
                                
                            self.env['logs.conciliacion'].create({
                                'texto': f"Actualizando descripción de {vals.name} de '{response.get('descripcion')}' a '{vals.display_name}'",
                                'modelo': 'product.product',
                                'fecha': fields.Datetime.now(),
                                'conciliacion_id': conciliacion_id
                            })
                    

                    if response.get('activo') != vals.active:
                        
                        if conciliacion_id:
                            self.env['logs.conciliacion'].create({
                                'texto': f"Actualizando estado de {vals.name} de {'Activo' if response.get('activo') else 'Inactivo'} a {'Activo' if vals.active else 'Inactivo'}",
                                'modelo': 'product.product',
                                'fecha': fields.Datetime.now(),
                                'conciliacion_id': conciliacion_id
                            })

                    resAPI = self.insertarProducto(vals);
                    self.env['logs.conciliacion'].create({
                        'texto': f"Respuesta de la API para {vals.name}: {resAPI}",
                        'modelo': 'product.product',
                        'fecha': fields.Datetime.now(),
                        'conciliacion_id': conciliacion_id
                    })
                    cantidad += 1;


                    vals.message_post(body=f"Se actualizó el producto en WIS. Respuesta de la API: {resAPI}")
            except Exception as e:
                self.env['logs.conciliacion'].create({
                    'texto': f"Error al procesar {vals.name}: {str(e)}",
                    'modelo': 'product.product',
                    'fecha': fields.Datetime.now(),
                    'conciliacion_id': vals.id
                })

        
        self.env['logs.conciliacion'].create({
            'texto': f"Se modificarón: {cantidad} productos",
            'modelo': 'product.product',
            'fecha': fields.Datetime.now(),
            'conciliacion_id': conciliacion_id
        })
        return;


    def conciliarStockProducto(self, variantes, conciliacion_id=None, ubicacionSalida=None, ubicacionDestino=None, diferenciaMinima=1):
        
        _logger.info("CAMPO DE CONCILIACION => " + str(conciliacion_id));

        if ubicacionDestino is None:
            ubicacionDestino = self.env['stock.location'].search([
                ('usage', '=', 'internal'),
                ('company_id', '=', self.env.company.id)
            ], limit=1)
        
        if ubicacionSalida is None:
            ubicacionSalida = self.env['stock.location'].search([
                ('usage', '=', 'internal'),
                ('company_id', '=', self.env.company.id)
            ], limit=1)

        if not variantes:
            raise ValidationError("No se han encontrado productos para conciliar stock")

        if conciliacion_id:
            self.env['logs.conciliacion.stock'].create({
                'texto': f"Comenzando la conciliación de stock para {len(variantes)} productos",
                'nivel': 'info',
                'fecha': fields.Datetime.now(),
                'conciliacion_id': conciliacion_id
            })

        for vals in variantes:
            cantidadActual = vals.qty_available
            
            try:
                cantidadWIS = self.consultaStock(vals)

                if cantidadWIS != cantidadActual:
                    if conciliacion_id:
                        self.env['logs.conciliacion.stock'].create({
                            'texto': f"El stock de {vals.name} es diferente. Actualizando stock en WIS de {cantidadWIS} a {cantidadActual}",
                            'nivel': 'info',
                            'fecha': fields.Datetime.now(),
                            'conciliacion_id': conciliacion_id
                        })

                    # Calcular la diferencia (positiva o negativa)
                    diferencia = cantidadActual - cantidadWIS
                    
                    diferencia = abs(diferencia);

                    if diferencia < diferenciaMinima:
                        if conciliacion_id:
                            self.env['logs.conciliacion.stock'].create({
                                'texto': f"La diferencia de stock para {vals.name} es menor que la mínima permitida ({diferenciaMinima}). No se realizará ninguna acción.",
                                'nivel': 'info',
                                'fecha': fields.Datetime.now(),
                                'conciliacion_id': conciliacion_id
                            })
                        continue

                   

                    picking_type = self.env['stock.picking.type'].search([
                        ('code', '=', 'internal'),
                        ('warehouse_id.company_id', '=', self.env.company.id)
                    ], limit=1)
                    
                    if not picking_type:
                        picking_type = self.env['stock.picking.type'].search([
                            ('warehouse_id.company_id', '=', self.env.company.id)
                        ], limit=1)
                    
                    if not picking_type:
                        raise ValidationError("No se encontró ningún tipo de picking disponible")

                    

                    try:
                        picking = self.env['stock.picking'].create({
                            'location_id': ubicacionSalida.id,
                            'location_dest_id': ubicacionDestino.id,
                            'picking_type_id': picking_type.id,
                            'move_type': 'direct',
                            'partner_id': self.partner.id, 
                            'origin': f"Conciliación de stock WIS - {vals.name}",
                            'company_id': self.env.company.id,
                        })

                        # Crear el movimiento de stock
                        move = self.env['stock.move'].create({
                            'name': f"Ajuste de stock: {vals.name}",
                            'product_id': vals.id,
                            'product_uom_qty': diferencia,
                            'product_uom': vals.uom_id.id,
                            'location_id': ubicacionSalida.id,
                            'location_dest_id': ubicacionDestino.id,
                            'picking_id': picking.id,
                            'company_id': self.env.company.id,
                        })

                        
                        # Marcar como hecho
                        for move_line in picking.move_line_ids:
                            move_line.qty_done = move_line.product_uom_qty
                        

                        if conciliacion_id:
                            self.env['logs.conciliacion.stock'].create({
                                'texto': f"Stock ajustado para {vals.name}: {cantidadWIS} → {cantidadActual} (diferencia: {diferencia})",
                                'nivel': 'info',
                                'fecha': fields.Datetime.now(),
                                'conciliacion_id': conciliacion_id
                            })

                    except Exception as pick_error:
                        if conciliacion_id:
                            self.env['logs.conciliacion.stock'].create({
                                'texto': f"Error al crear picking para {vals.name}: {str(pick_error)}",
                                'nivel': 'error',
                                'fecha': fields.Datetime.now(),
                                'conciliacion_id': conciliacion_id
                            })
                        raise pick_error
                        
            except Exception as e:
                if conciliacion_id:
                    self.env['logs.conciliacion.stock'].create({
                        'texto': f"Error al procesar {vals.name}: {str(e)}",
                        'nivel': 'error',
                        'fecha': fields.Datetime.now(),
                        'conciliacion_id': conciliacion_id
                    })
                continue

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