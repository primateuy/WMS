from odoo import fields, api, models
from odoo.exceptions import UserError, ValidationError
import logging
import json

_logger = logging.getLogger(__name__)

class WMSEventProcessor(models.TransientModel):
    _name = "wms.event.processor"
    _description = "Proceso de los eventos en respuesta al WebHook"

    def confirmacionMercaderiaPreparada(self, payload):
        self.env['wms.pedido.evento'].create({
            'tipoEvento': 'Confirmación de mercaderia preparada',
            'fechaHora': fields.Datetime.now(),
            'datosRecibidos': payload
        })

        picking = self.env['stock.picking'].search([
                ('idPedidoWMS', '=', payload.get('idPedidoWMS'))
            ], limit=1);

        boolActivado = picking.picking_type_id.metodo_preparacion_wis;

        if not boolActivado:
            _logger.info("El metodo de preparacion de WMS no se encuentra habilitado");
            return;

        try:
            picking.state = 'preparado_wms';
            picking.message_post(body=f"El producto ya se ha sido preparado. {fields.Datetime.now()}")

        except Exception as e:
            _logger.info(e);
            return False;

    def confirmacionPedido(self, payload):
        self.env['wms.pedido.evento'].create({
            'tipoEvento': 'Confirmación de pedido',
            'fechaHora': fields.Datetime.now(),
            'datosRecibidos': payload
        });

        try:
            picking = self.env['stock.picking'].search([
                ('idPedidoWMS', '=', payload.get('idPedidoWMS'))
            ], limit=1)
            
            if not picking:
                _logger.error(f"No se encontró picking con idPedidoWMS: {payload.get('idPedidoWMS')}")
                return False

            if self._es_operacion_desempaquetado(payload):
                _logger.info(f"Procesando desempaquetado para picking {picking.name}")
                resultado_desempaquetado = self._procesar_desempaquetado_completo(payload)
                if not resultado_desempaquetado['success']:
                    _logger.error(f"Error en desempaquetado: {resultado_desempaquetado['error']}")
                    return False
                
                if payload.get('soloDesempaquetado', False):
                    return True

            if payload.get('contenedores'):
                _logger.info(f"Procesando empaquetado para picking {picking.name}")
                resultado_empaquetado = self._procesar_empaquetado_completo(picking, payload)
                if not resultado_empaquetado['success']:
                    _logger.error(f"Error en empaquetado: {resultado_empaquetado['error']}")
                    return False

            if picking.state != 'done':
                picking.button_validate()

            picking.write({
                'transportadora': payload.get('transportadora'),
                'wms_dispatch_date': fields.Datetime.now(),
                'wms_empaquetado': True if payload.get('contenedores') else False
            });

            if payload.get('pedidos'):
                self._procesar_pedidos_adicionales(picking, payload['pedidos'])
       
            _logger.info(f"Picking {picking.name} confirmado como despachado")
            return True
            
        except Exception as e:
            _logger.error(f"Error en confirmacionPedido: {str(e)}")
            return False

    def _es_operacion_desempaquetado(self, payload):
        if payload.get('tipoOperacion') == 'desempaquetado':
            return True
        
        if payload.get('aperturaPaquetes'):
            return True
            
        if payload.get('paquetesAbiertos'):
            return True
            
        contenedores = payload.get('contenedores', [])
        for contenedor in contenedores:
            if contenedor.get('operacion') == 'apertura':
                return True
            if contenedor.get('abierto') == True:
                return True
                
        if payload.get('codigoPaquete') and payload.get('productos'):
            return True
            
        return False

    def _procesar_desempaquetado_completo(self, payload):
        try:
            paquetes_procesados = 0
            
            if payload.get('codigoPaquete'):
                resultado = self._procesar_desempaquetado_individual(payload)
                if resultado['success']:
                    paquetes_procesados += 1
                else:
                    return resultado

            if payload.get('aperturaPaquetes'):
                for paquete_data in payload['aperturaPaquetes']:
                    resultado = self._procesar_desempaquetado_individual(paquete_data)
                    if resultado['success']:
                        paquetes_procesados += 1
                    else:
                        _logger.error(f"Error en paquete {paquete_data.get('codigoPaquete')}: {resultado['error']}")

            if payload.get('contenedores'):
                for contenedor in payload['contenedores']:
                    if contenedor.get('operacion') == 'apertura' or contenedor.get('abierto'):
                        paquete_data = {
                            'codigoPaquete': contenedor.get('numeroContenedor') or contenedor.get('codigoBulto'),
                            'productos': contenedor.get('productos', contenedor.get('detalles', []))
                        }
                        resultado = self._procesar_desempaquetado_individual(paquete_data)
                        if resultado['success']:
                            paquetes_procesados += 1

            self._registrar_evento_desempaquetado(payload, paquetes_procesados)

            return {
                'success': True,
                'paquetes_procesados': paquetes_procesados,
                'message': f'Desempaquetado procesado exitosamente: {paquetes_procesados} paquetes'
            }

        except Exception as e:
            _logger.error(f"Error en desempaquetado completo: {str(e)}")
            return {'success': False, 'error': str(e)}

    def _procesar_desempaquetado_individual(self, paquete_data):
        try:
            codigo_paquete = paquete_data.get('codigoPaquete')
            if not codigo_paquete:
                return {'success': False, 'error': 'Código de paquete requerido'}

            paquete = self._buscar_paquete(codigo_paquete)
            if not paquete:
                return {'success': False, 'error': f'Paquete no encontrado: {codigo_paquete}'}

            if hasattr(paquete, 'cerrado') and not paquete.cerrado:
                _logger.warning(f"Paquete {codigo_paquete} ya estaba abierto")

            productos_esperados = paquete_data.get('productos', [])
            if productos_esperados:
                validacion = self._validar_productos_desempaquetado(paquete, productos_esperados)
                if not validacion['success']:
                    return validacion

            resultado_apertura = self._abrir_paquete(paquete, paquete_data)
            if not resultado_apertura['success']:
                return resultado_apertura

            self._actualizar_estado_paquete_abierto(paquete, paquete_data)

            return {
                'success': True,
                'paquete': codigo_paquete,
                'productos_liberados': len(productos_esperados) if productos_esperados else len(paquete.quant_ids)
            }

        except Exception as e:
            _logger.error(f"Error procesando desempaquetado individual: {str(e)}")
            return {'success': False, 'error': str(e)}

    def _buscar_paquete(self, codigo_paquete):
        Package = self.env['stock.quant.package']
        
        if hasattr(Package, 'codigo_wis'):
            paquete = Package.search([('codigo_wis', '=', codigo_paquete)], limit=1)
            if paquete:
                return paquete

        paquete = Package.search([('name', '=', codigo_paquete)], limit=1)
        if paquete:
            return paquete

        if hasattr(Package, 'barcode'):
            paquete = Package.search([('barcode', '=', codigo_paquete)], limit=1)
            if paquete:
                return paquete

        return False

    def _validar_productos_desempaquetado(self, paquete, productos_esperados):
        try:
            productos_en_paquete = {}
            for quant in paquete.quant_ids:
                sku = quant.product_id.default_code
                if sku in productos_en_paquete:
                    productos_en_paquete[sku] += quant.quantity
                else:
                    productos_en_paquete[sku] = quant.quantity

            # Validar cada producto esperado
            discrepancias = []
            for producto_data in productos_esperados:
                sku = producto_data.get('producto') or producto_data.get('sku')
                cantidad_esperada = float(producto_data.get('cantidad', producto_data.get('cantidades', 0)))
                
                cantidad_real = productos_en_paquete.get(sku, 0)
                
                if cantidad_real != cantidad_esperada:
                    discrepancias.append({
                        'sku': sku,
                        'esperado': cantidad_esperada,
                        'real': cantidad_real
                    })

            if discrepancias:
                mensaje_error = f"Discrepancias en paquete {paquete.name}: {discrepancias}"
                _logger.warning(mensaje_error)
                
                continuar_con_discrepancias = self.env['ir.config_parameter'].sudo().get_param(
                    'wms.permitir_discrepancias_desempaquetado', 'True'
                ) == 'True'
                
                if not continuar_con_discrepancias:
                    return {'success': False, 'error': mensaje_error}

            return {'success': True}

        except Exception as e:
            return {'success': False, 'error': f'Error validando productos: {str(e)}'}

    def _abrir_paquete(self, paquete, paquete_data):
        try:
            metodo_apertura = self.env['ir.config_parameter'].sudo().get_param(
                'wms.metodo_apertura_paquete', 'disociar'  # 'disociar' o 'eliminar'
            )

            if metodo_apertura == 'eliminar':
                return self._eliminar_paquete(paquete)
            else:
                return self._disociar_productos_paquete(paquete)

        except Exception as e:
            return {'success': False, 'error': f'Error abriendo paquete: {str(e)}'}

    def _disociar_productos_paquete(self, paquete):
        try:
            productos_liberados = 0
            
            quants_en_paquete = paquete.quant_ids
            
            for quant in quants_en_paquete:
                quant.write({'package_id': False})
                productos_liberados += 1

            move_lines = self.env['stock.move.line'].search([
                ('result_package_id', '=', paquete.id)
            ])
            
            for move_line in move_lines:
                move_line.write({'result_package_id': False})

            _logger.info(f"Paquete {paquete.name} abierto: {productos_liberados} productos liberados")
            
            return {
                'success': True,
                'productos_liberados': productos_liberados,
                'metodo': 'disociacion'
            }

        except Exception as e:
            return {'success': False, 'error': f'Error disociando productos: {str(e)}'}

    def _eliminar_paquete(self, paquete):
        try:
            productos_liberados = len(paquete.quant_ids)
            nombre_paquete = paquete.name

            paquete.unlink()

            _logger.info(f"Paquete {nombre_paquete} eliminado: {productos_liberados} productos liberados")
            
            return {
                'success': True,
                'productos_liberados': productos_liberados,
                'metodo': 'eliminacion'
            }

        except Exception as e:
            return {'success': False, 'error': f'Error eliminando paquete: {str(e)}'}

    def _actualizar_estado_paquete_abierto(self, paquete, paquete_data):
        try:
            if paquete.exists():
                datos_actualizacion = {
                    'cerrado': False,
                }

                if hasattr(paquete, 'fecha_apertura'):
                    datos_actualizacion['fecha_apertura'] = fields.Datetime.now()
                
                if hasattr(paquete, 'operador_apertura'):
                    datos_actualizacion['operador_apertura'] = paquete_data.get('operador')

                if hasattr(paquete, 'motivo_apertura'):
                    datos_actualizacion['motivo_apertura'] = paquete_data.get('motivo', 'Redistribución WMS')

                paquete.write(datos_actualizacion)

                # Agregar mensaje al paquete
                paquete.message_post(
                    body=f"Paquete abierto por WMS - {fields.Datetime.now()}"
                )

        except Exception as e:
            _logger.error(f"Error actualizando estado de paquete abierto: {str(e)}")

    def _registrar_evento_desempaquetado(self, payload, num_paquetes):
        try:
            self.env['wms.pedido.evento'].create({
                'tipoEvento': 'Desempaquetado/Apertura',
                'fechaHora': fields.Datetime.now(),
                'datosRecibidos': f"Paquetes abiertos: {num_paquetes}. Payload: {json.dumps(payload)[:500]}...",
                'estado': 'procesado',
                'operador': payload.get('operador'),
                'observaciones': f'Se abrieron {num_paquetes} paquetes exitosamente'
            })

        except Exception as e:
            _logger.error(f"Error registrando evento de desempaquetado: {str(e)}")


    def confirmacionDesempaquetado(self, payload):
        self.env['wms.pedido.evento'].create({
            'tipoEvento': 'Confirmación de desempaquetado',
            'fechaHora': fields.Datetime.now(),
            'datosRecibidos': payload
        })

        try:
            resultado = self._procesar_desempaquetado_completo(payload)
            
            if resultado['success']:
                _logger.info(f"Desempaquetado completado: {resultado['paquetes_procesados']} paquetes")
                return True
            else:
                _logger.error(f"Error en desempaquetado: {resultado['error']}")
                return False

        except Exception as e:
            _logger.error(f"Error en confirmacionDesempaquetado: {str(e)}")
            return False

    def aperturaDeOtrosPaquetes(self, payload):
        return self.confirmacionDesempaquetado(payload)


    def consultar_estado_paquete(self, codigo_paquete):
        try:
            paquete = self._buscar_paquete(codigo_paquete)
            
            if not paquete:
                return {'encontrado': False, 'error': 'Paquete no encontrado'}

            estado = {
                'encontrado': True,
                'nombre': paquete.name,
                'cerrado': getattr(paquete, 'cerrado', True),
                'productos': len(paquete.quant_ids),
                'peso': getattr(paquete, 'peso_declarado', 0.0),
            }

            # Información de productos
            productos_info = []
            for quant in paquete.quant_ids:
                productos_info.append({
                    'sku': quant.product_id.default_code,
                    'nombre': quant.product_id.name,
                    'cantidad': quant.quantity,
                    'ubicacion': quant.location_id.name
                })

            estado['detalle_productos'] = productos_info

            return estado

        except Exception as e:
            return {'encontrado': False, 'error': str(e)}

    def _procesar_empaquetado_completo(self, picking, payload):
        try:
            contenedores = payload.get('contenedores', [])
            if not contenedores:
                return {'success': True, 'message': 'No hay contenedores para procesar'}

            paquetes_creados = []
            
            for i, contenedor in enumerate(contenedores):
                resultado = self._crear_paquete_mejorado(picking, contenedor, payload, i)
                if resultado['success']:
                    paquetes_creados.append(resultado['paquete'])
                else:
                    return {'success': False, 'error': resultado['error']}

            self._registrar_evento_empaquetado(picking, payload, len(paquetes_creados))

            return {
                'success': True, 
                'paquetes_creados': len(paquetes_creados),
                'message': f'Empaquetado procesado exitosamente: {len(paquetes_creados)} paquetes'
            }

        except Exception as e:
            _logger.error(f"Error en empaquetado completo: {str(e)}")
            return {'success': False, 'error': str(e)}

    def _crear_paquete_mejorado(self, picking, contenedor, payload, index):
        try:
            codigo_contenedor = contenedor.get('numeroContenedor') or contenedor.get('codigoBulto')
            if not codigo_contenedor:
                codigo_contenedor = f"PKG-{picking.name}-{index+1}"

            Package = self.env['stock.quant.package']
            paquete_existente = Package.search([
                ('name', '=', codigo_contenedor)
            ], limit=1)

            if paquete_existente:
                _logger.warning(f"Paquete {codigo_contenedor} ya existe, actualizando...")
                package = paquete_existente
            else:
                datos_paquete = {
                    'name': codigo_contenedor,
                    'picking_origen_id': picking.id,
                }

                if hasattr(Package, 'codigo_wis'):
                    datos_paquete.update({
                        'codigo_wis': codigo_contenedor,
                        'transportadora': payload.get('transportadora'),
                        'operador_empaque': payload.get('operador'),
                        'fecha_empaque': fields.Datetime.now(),
                        'peso_declarado': contenedor.get('peso', 0.0),
                        'tipo_contenedor': self._mapear_tipo_contenedor(contenedor.get('tipoContenedor')),
                        'cerrado': contenedor.get('cerrado', True),
                    })

                    # Dimensiones si existen
                    dimensiones = contenedor.get('dimensiones', {})
                    if dimensiones:
                        datos_paquete.update({
                            'largo': dimensiones.get('largo', 0.0),
                            'ancho': dimensiones.get('ancho', 0.0),
                            'alto': dimensiones.get('alto', 0.0),
                        })

                package = Package.create(datos_paquete)

            detalles = contenedor.get('detalles', [])
            productos = contenedor.get('productos', [])  # Para compatibilidad con ambos formatos
            
            items_a_procesar = detalles if detalles else productos
            
            for detalle in items_a_procesar:
                resultado_producto = self._procesar_producto_en_paquete(picking, package, detalle)
                if not resultado_producto:
                    return {'success': False, 'error': f'Error procesando producto en contenedor {codigo_contenedor}'}

            return {'success': True, 'paquete': package}

        except Exception as e:
            _logger.error(f"Error creando paquete {index+1}: {str(e)}")
            return {'success': False, 'error': str(e)}

    def _procesar_producto_en_paquete(self, picking, package, detalle):
        try:
            codigo_producto = detalle.get('producto') or detalle.get('sku') or detalle.get('codigoProducto')
            cantidad = float(detalle.get('cantidadPreparada', detalle.get('cantidad', 0)))
            lote = detalle.get('lote')

            if not codigo_producto or cantidad <= 0:
                _logger.warning(f"Producto inválido en paquete: {detalle}")
                return True  # Continuar con otros productos

            # Buscar el producto
            producto = self.env['product.product'].search([
                ('default_code', '=', codigo_producto)
            ], limit=1)

            if not producto:
                _logger.error(f"Producto no encontrado: {codigo_producto}")
                return False

            move = self.env['stock.move'].search([
                ('picking_id', '=', picking.id),
                ('product_id', '=', producto.id)
            ], limit=1)

            if not move:
                move = self.env['stock.move'].create({
                    'name': producto.name,
                    'product_id': producto.id,
                    'product_uom_qty': cantidad,
                    'product_uom': producto.uom_id.id,
                    'location_id': picking.location_id.id,
                    'location_dest_id': picking.location_dest_id.id,
                    'picking_id': picking.id,
                    'state': 'draft',
                })

            move_line_data = {
                'product_id': producto.id,
                'product_uom_id': producto.uom_id.id,
                'qty_done': cantidad,
                'result_package_id': package.id,
                'location_id': picking.location_id.id,
                'location_dest_id': picking.location_dest_id.id,
                'move_id': move.id,
                'picking_id': picking.id,
            }

            if lote:
                lote_obj = self._obtener_o_crear_lote(producto, lote)
                if lote_obj:
                    move_line_data['lot_id'] = lote_obj.id

            existing_line = self.env['stock.move.line'].search([
                ('move_id', '=', move.id),
                ('product_id', '=', producto.id),
                ('result_package_id', '=', False)  # Solo líneas sin paquete asignado
            ], limit=1)

            if existing_line:
                existing_line.write({
                    'qty_done': cantidad,
                    'result_package_id': package.id
                })
            else:
                self.env['stock.move.line'].create(move_line_data)

            return True

        except Exception as e:
            _logger.error(f"Error procesando producto en paquete: {str(e)}")
            return False

    def _obtener_o_crear_lote(self, producto, nombre_lote):
        try:
            lote = self.env['stock.production.lot'].search([
                ('name', '=', nombre_lote),
                ('product_id', '=', producto.id)
            ], limit=1)

            if not lote:
                lote = self.env['stock.production.lot'].create({
                    'name': nombre_lote,
                    'product_id': producto.id,
                })

            return lote

        except Exception as e:
            _logger.error(f"Error con lote {nombre_lote}: {str(e)}")
            return False

    def _mapear_tipo_contenedor(self, tipo_wis):
        if not tipo_wis:
            return 'otro'
            
        mapeo = {
            'CAJA_ESTANDAR': 'caja_estandar',
            'CAJA_GRANDE': 'caja_grande', 
            'SOBRE': 'sobre',
            'PALLET': 'pallet',
            'CAJA': 'caja_estandar',
            'BOX': 'caja_estandar',
        }
        return mapeo.get(tipo_wis.upper(), 'otro')

    def _procesar_pedidos_adicionales(self, picking, pedidos):
        try:
            for pedido in pedidos:
                for detalle in pedido.get('detalles', []):
                    producto = self.env['product.product'].search([
                        ('default_code', '=', detalle['producto'])
                    ], limit=1)
                    
                    if producto:
                        existing_move = self.env['stock.move'].search([
                            ('picking_id', '=', picking.id),
                            ('product_id', '=', producto.id)
                        ], limit=1)

                        if not existing_move:
                            self.env['stock.move'].create({
                                'name': producto.name,
                                'product_id': producto.id,
                                'product_uom_qty': detalle['cantidadProducto'],
                                'product_uom': producto.uom_id.id,
                                'location_id': picking.location_id.id,
                                'location_dest_id': picking.location_dest_id.id,
                                'picking_id': picking.id,
                                'state': 'draft',
                            })

        except Exception as e:
            _logger.error(f"Error procesando pedidos adicionales: {str(e)}")

    def _registrar_evento_empaquetado(self, picking, payload, num_paquetes):
        
        try:
            self.env['wms.pedido.evento'].create({
                'tipoEvento': 'Empaquetado completado',
                'fechaHora': fields.Datetime.now(),
                'datosRecibidos': f"Paquetes creados: {num_paquetes}. Payload: {json.dumps(payload)[:500]}..."
            })
            
            picking.message_post(
                body=f"Empaquetado WMS completado - {num_paquetes} paquetes creados. {fields.Datetime.now()}"
            )

        except Exception as e:
            _logger.error(f"Error registrando evento de empaquetado: {str(e)}")

   

    def pedidosAnulados(self, payload):
        self.env['wms.pedido.evento'].create({
            'tipoEvento': 'Pedido anulado',
            'fechaHora': fields.Datetime.now(),
            'datosRecibidos': payload
        })

        try:
            picking = self.env['stock.picking'].search([
                ('idPedidoWMS', '=', payload.get('idPedidoWMS'))
            ], limit=1)

            if picking and picking.state != 'done':
                picking.action_cancel()

        except Exception as e:
            _logger.info(e)

    def confirmacionRecepcion(self, payload):
        if not payload.get('idPedidoWMS'):
            return {"error": "idPedidoWMS es requerido"}

        ordenCompra = self.env['purchase.order'].search([
            ('idPedidoWMS', '=', payload.get('idPedidoWMS'))
        ], limit=1)
        
        if not ordenCompra:
            return {"error": f"Orden de compra con idPedidoWMS {payload.get('idPedidoWMS')} no encontrada"}

        productos = payload.get('productos', []);
        
        for producto_data in productos:
            producto_codigo = producto_data.get('producto')
            cantidad_recibida = float(producto_data.get('cantidadRecibida', 0))
            
            producto = self.env['product.product'].search([
                ('default_code', '=', producto_codigo)
            ], limit=1)
            
            if not producto:
                continue  
            
            linea_oc = ordenCompra.order_line.filtered(
                lambda l: l.product_id == producto
            )
            
            if linea_oc:
                linea_oc.qty_received = cantidad_recibida 

        ordenCompra.message_post(
            body=f"Recepción actualizada desde WMS. Productos recibidos: {len(productos)}"
        )

        ordenCompra.button_validate();

        return {
            "success": True,
            "purchase_order": ordenCompra.name,
            "updated_lines": len(productos)
        }
    
    def ajustes(self, payload):
        _logger.info("Entrando a la respuestas de ajustes para el WebHook");

        consultaStock = payload.get('consultaStock');
        productos = self.env['product.product'].search([('active', '=', True)])

        for stock in consultaStock.stock:
            for producto in productos:
                if stock.producto == producto.id and producto.product_qty != stock.cantidad:
                    cantidadAntigua = producto.product_qty

                    if producto.ajusteManualStock:
                        producto.message_post(
                            body=f"La cantidad de ajuste de stock no coincide con lo devuelto en WIS, se sugiere cambiarlo a: {producto.product_qty}"
                        )
                    else:
                        producto.message_post(
                            body=f"Se hizó una actualización en el stock de este producto, puesto que no coincidia con la cantidad de WIS: {cantidadAntigua} -> {producto.product_qty}"
                        )
                        producto.product_qty = stock.cantidad;

  
    def confirmacionEmpaquetado(self, payload):
        
        self.env['wms.pedido.evento'].create({
            'tipoEvento': 'Confirmación de empaquetado',
            'fechaHora': fields.Datetime.now(),
            'datosRecibidos': payload
        })

        try:
            picking = self.env['stock.picking'].search([
                ('idPedidoWMS', '=', payload.get('idPedidoWMS'))
            ], limit=1)
            
            if not picking:
                _logger.error(f"No se encontró picking para empaquetado: {payload.get('idPedidoWMS')}")
                return False

            resultado = self._procesar_empaquetado_completo(picking, payload)
            
            if resultado['success']:
                picking.write({'wms_empaquetado': True})
                _logger.info(f"Empaquetado completado para picking {picking.name}")
                return True
            else:
                _logger.error(f"Error en empaquetado: {resultado['error']}")
                return False

        except Exception as e:
            _logger.error(f"Error en confirmacionEmpaquetado: {str(e)}")
            return False