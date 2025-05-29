import os
import logging
import oracledb
from flask import Flask, request, jsonify
from flask_cors import CORS
from contextlib import contextmanager
from decimal import Decimal, ROUND_HALF_UP # Importado para precisión
import traceback
from datetime import datetime, date

# --- Configuración de Logging y Oracle Client ---
python_init_logger = logging.getLogger("PYTHON_INIT")
python_init_logger.setLevel(logging.INFO)
if not python_init_logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    python_init_logger.addHandler(handler)

instant_client_dir_path = os.environ.get('ORACLE_CLIENT_LIB_DIR')
try:
    if instant_client_dir_path:
        python_init_logger.info(f"Intentando inicializar Oracle Client (Modo Thick) desde: {instant_client_dir_path}")
        oracledb.init_oracle_client(lib_dir=instant_client_dir_path)
    else:
        python_init_logger.info("ORACLE_CLIENT_LIB_DIR no configurado. Intentando inicializar Oracle Client (Modo Thick) usando PATH del sistema o configuración por defecto...")
        oracledb.init_oracle_client()
    python_init_logger.info(f"Oracle Client (Modo Thick) inicializado exitosamente. Version: {'.'.join(map(str, oracledb.clientversion()))}")
except Exception as e:
    python_init_logger.warning(f"ADVERTENCIA PYTHON_INIT: No se pudo inicializar Oracle Client (Modo Thick): {e}")
    python_init_logger.warning("PYTHON_INIT: La aplicación intentará usar el modo Thin de python-oracledb si está disponible o falla.")

app = Flask(__name__)
CORS(app)
app.logger.setLevel(logging.INFO)
if not app.logger.handlers:
    flask_handler = logging.StreamHandler()
    flask_formatter = logging.Formatter('%(asctime)s - %(name)s (Flask) - %(levelname)s - %(message)s')
    flask_handler.setFormatter(flask_formatter)
    app.logger.addHandler(flask_handler)
    app.logger.propagate = False

DEBUG_MODE = os.environ.get('DEBUG_MODE', 'true').lower() == 'true'
DB_USER = os.environ.get("DB_USER", "admin") # Default user for local dev if not set
DB_PASSWORD = os.environ.get("DB_PASSWORD", "PasswordCafe123") # Default pass for local dev
DB_DSN = os.environ.get("DB_DSN", "localhost:1521/XEPDB1") # Default DSN for local dev
TNS_ADMIN = os.environ.get('TNS_ADMIN')
DB_WALLET_PASSWORD=os.environ.get('123456789f')

POOL_MIN = int(os.environ.get('DB_POOL_MIN', '2'))
POOL_MAX = int(os.environ.get('DB_POOL_MAX', '10'))
POOL_INCREMENT = int(os.environ.get('DB_POOL_INCREMENT', '1'))
POOL_TIMEOUT = int(os.environ.get('DB_POOL_TIMEOUT', '30'))
POOL_MAX_LIFETIME_SESSION = int(os.environ.get('DB_POOL_MAX_LIFETIME_SESSION', '3600'))

if DEBUG_MODE:
    app.logger.info("=== MODO DEBUG ACTIVADO (Variables de Entorno) ===")
    app.logger.info(f"TNS_ADMIN: {TNS_ADMIN}")
    app.logger.info(f"DB_USER: {DB_USER}")
    app.logger.info(f"DB_DSN: {DB_DSN}")
    app.logger.info(f"DB_PASSWORD configurada: {'Sí' if DB_PASSWORD else 'No'}")


def validate_config():
    missing = []
    if not DB_USER: missing.append("DB_USER")
    if not DB_PASSWORD: missing.append("DB_PASSWORD")
    if not DB_DSN: missing.append("DB_DSN")
    if missing:
        error_msg = f"Variables de entorno faltantes para la conexión a DB: {', '.join(missing)}"
        app.logger.critical(error_msg)
        return False, error_msg
    return True, "Configuración de variables de entorno de DB parece válida."

config_valid, config_msg = validate_config()
if not config_valid:
    app.logger.critical("APLICACIÓN NO PUEDE INICIARSE CORRECTAMENTE CON LA BASE DE DATOS: " + config_msg)

pool = None
pool_error = None

def _output_type_handler_varchar_strip(cursor, name, default_type, size, precision, scale):
    if default_type == oracledb.DB_TYPE_VARCHAR:
        return cursor.var(str, arraysize=cursor.arraysize, outconverter=lambda v: v.strip() if v is not None else None)
    return None

def _session_callback_set_handlers(conn, requested_tag):
    # conn.outputtypehandler = _output_type_handler_varchar_strip # Se aplica por conexión ahora
    pass

def create_connection_pool():
    global pool, pool_error
    if not config_valid:
        pool_error = f"Configuración de DB inválida ({config_msg}), no se puede crear pool."
        app.logger.error(f"CREATE_POOL_FUNC: {pool_error}")
        return False

    try:
        app.logger.info(f"CREATE_POOL_FUNC: Creando pool de conexiones a Oracle. DSN='{DB_DSN}', User='{DB_USER}'")
        app.logger.info(f"CREATE_POOL_FUNC: Configuración del pool: min={POOL_MIN}, max={POOL_MAX}, increment={POOL_INCREMENT}, timeout={POOL_TIMEOUT}, max_lifetime_session={POOL_MAX_LIFETIME_SESSION}")

       # params = {}
       # if TNS_ADMIN:
       #     params["config_dir"] = TNS_ADMIN
       #     params["wallet_location"] = TNS_ADMIN
       #     if DB_WALLET_PASSWORD:
       #         params["wallet_password"] = DB_WALLET_PASSWORD

        pool = oracledb.create_pool(
            user=DB_USER, password=DB_PASSWORD, dsn=DB_DSN,
            min=POOL_MIN, max=POOL_MAX, increment=POOL_INCREMENT,
            timeout=POOL_TIMEOUT,
            max_lifetime_session=POOL_MAX_LIFETIME_SESSION,
            session_callback=_session_callback_set_handlers,

        )
        app.logger.info(f"CREATE_POOL_FUNC: Pool creado exitosamente.")
        pool_error = None
        return True
    except oracledb.DatabaseError as e:
        error_obj, = e.args
        error_msg = f"Error de Oracle al crear pool: {error_obj.message} (Code: {error_obj.code}, Offset: {error_obj.offset})"
        app.logger.error(f"CREATE_POOL_FUNC: {error_msg}")
        if DEBUG_MODE: app.logger.error(traceback.format_exc())
        pool_error = error_msg
        return False
    except Exception as e:
        error_msg = f"Error inesperado al crear pool: {e}"
        app.logger.error(f"CREATE_POOL_FUNC: {error_msg}")
        if DEBUG_MODE: app.logger.error(traceback.format_exc())
        pool_error = error_msg
        return False

if config_valid:
    create_connection_pool()
else:
    pool_error = "Configuración de DB inválida, no se intentó crear el pool."
    app.logger.error(pool_error)

@contextmanager
def get_db_connection():
    if not pool:
        current_error_msg = pool_error if pool_error else config_msg if not config_valid else "Pool no inicializado por razón desconocida."
        app.logger.error(f"GET_DB_CONNECTION: Pool no disponible. Error: {current_error_msg}")
        raise Exception(f"Pool de conexiones no disponible: {current_error_msg}")

    connection = None
    try:
        connection = pool.acquire()
        connection.outputtypehandler = _output_type_handler_varchar_strip
        yield connection
    except oracledb.DatabaseError as e:
        error_obj, = e.args
        app.logger.error(f"GET_DB_CONNECTION: Error de Oracle adquiriendo/usando conexión: {error_obj.message}")
        raise
    except Exception as e:
        app.logger.error(f"GET_DB_CONNECTION: Error genérico adquiriendo/usando conexión: {e}")
        raise
    finally:
        if connection:
            try:
                pool.release(connection)
            except Exception as rel_e:
                app.logger.error(f"GET_DB_CONNECTION: Error liberando conexión: {rel_e}")

def _row_to_dict(cursor, row):
    if row is None: return None
    column_names = [d[0].lower() for d in cursor.description]
    return dict(zip(column_names, row))

def convert_data_types(obj):
    if isinstance(obj, dict):
        return {key: convert_data_types(value) for key, value in obj.items()}
    elif isinstance(obj, list):
        return [convert_data_types(item) for item in obj]
    elif isinstance(obj, Decimal):
        # Convertir a float solo si no tiene más de 2-3 decimales o si es para JSON directo
        # Para cálculos internos, mantener Decimal. Para JSON, float está bien.
        return float(obj)
    elif isinstance(obj, (datetime, date)):
        return obj.isoformat()
    else:
        return obj

# --- Funciones de Validación ---
def validate_ingrediente_data(data, is_update=False):
    errors = []
    if not is_update or 'nombre' in data:
        nombre = data.get('nombre', '').strip()
        if not nombre: errors.append("El nombre del ingrediente es requerido.")
        elif len(nombre) > 100: errors.append("El nombre no debe exceder 100 caracteres.")

    if not is_update or 'unidad_medida' in data:
        unidad = data.get('unidad_medida', '').strip()
        if not unidad: errors.append("La unidad de medida es requerida.")
        elif len(unidad) > 50: errors.append("La unidad no debe exceder 50 caracteres.")

    if 'descripcion' in data and data.get('descripcion') is not None:
        if len(data['descripcion']) > 255: errors.append("La descripción no debe exceder 255 caracteres.")

    for field in ['stock_actual', 'stock_minimo']:
        if field in data:
            value_str = str(data.get(field, '')).strip() # Usar get para evitar KeyError si el campo falta
            if not value_str and (data.get(field) is not None):
                 errors.append(f"{field.replace('_', ' ').title()} no puede estar vacío si se incluye.")
                 continue
            if data.get(field) is None and not is_update :
                 errors.append(f"{field.replace('_', ' ').title()} es requerido.")
                 continue
            if data.get(field) is not None:
                try:
                    # Usar Decimal para validar, pero el valor en 'data' puede ser float o str del JSON
                    value = Decimal(str(data[field]))
                    if value < 0: errors.append(f"{field.replace('_', ' ').title()} no puede ser negativo.")
                    # Ajustar la validación de precisión si es necesario, ej. NUMBER(10,3)
                    if value.as_tuple().exponent < -3: errors.append(f"{field.replace('_', ' ').title()} excede la precisión decimal permitida (3 decimales).")
                    if value > Decimal('9999999.999'): errors.append(f"{field.replace('_', ' ').title()} excede el valor máximo.")

                except Exception: # Captura errores de conversión de Decimal también
                    errors.append(f"{field.replace('_', ' ').title()} debe ser un número válido.")
    return errors

def validate_producto_data(data, is_update=False):
    errors = []
    if not is_update or 'nombre' in data:
        nombre = data.get('nombre', '').strip()
        if not nombre: errors.append("El nombre del producto es requerido.")
        elif len(nombre) > 100: errors.append("El nombre del producto no debe exceder 100 caracteres.")

    if not is_update or 'precio_venta' in data:
        precio_str = str(data.get('precio_venta', '')).strip()
        if not precio_str and not is_update:
            errors.append("El precio de venta es requerido.")
        elif data.get('precio_venta') is None and not is_update:
             errors.append("El precio de venta es requerido y no puede ser nulo.")
        elif data.get('precio_venta') is not None:
            try:
                precio = Decimal(str(data['precio_venta']))
                if precio < 0: errors.append("El precio de venta no puede ser negativo.")
                if precio > Decimal('99999999.99'): errors.append("El precio de venta excede el valor máximo.")
            except Exception: errors.append("El precio de venta debe ser un número válido.")

    if 'descripcion' in data and data.get('descripcion') is not None:
        if len(data['descripcion']) > 255: errors.append("La descripción del producto no debe exceder 255 caracteres.")

    if 'categoria' in data and data.get('categoria') is not None:
        if len(data['categoria']) > 100: errors.append("La categoría del producto no debe exceder 100 caracteres.")

    if 'ingredientes' in data:
        if not isinstance(data['ingredientes'], list):
            errors.append("Los ingredientes deben ser una lista.")
        else:
            for idx, item_receta in enumerate(data['ingredientes']):
                if not isinstance(item_receta, dict):
                    errors.append(f"El ingrediente #{idx+1} de la receta no es un objeto válido.")
                    continue
                if "id_ingrediente" not in item_receta: errors.append(f"Falta 'id_ingrediente' para el ingrediente #{idx+1}.")
                else:
                    try: int(item_receta["id_ingrediente"])
                    except (ValueError, TypeError): errors.append(f"'id_ingrediente' para #{idx+1} debe ser entero.")

                if "cantidad_necesaria" not in item_receta: errors.append(f"Falta 'cantidad_necesaria' para el ingrediente #{idx+1}.")
                else:
                    try:
                        cant = Decimal(str(item_receta["cantidad_necesaria"]))
                        if cant <= 0: errors.append(f"'cantidad_necesaria' para #{idx+1} debe ser positiva.")
                        if cant.as_tuple().exponent < -3: errors.append(f"'cantidad_necesaria' para #{idx+1} excede precisión (3 decimales).")
                        if cant > Decimal('99999.999'): errors.append(f"'cantidad_necesaria' para #{idx+1} excede el máximo.")
                    except Exception: errors.append(f"'cantidad_necesaria' para #{idx+1} debe ser un número.")

                if "unidad_medida_receta" not in item_receta or not str(item_receta.get("unidad_medida_receta","")).strip():
                    errors.append(f"Falta 'unidad_medida_receta' para el ingrediente #{idx+1}.")
                elif len(str(item_receta["unidad_medida_receta"])) > 50:
                    errors.append(f"'unidad_medida_receta' para #{idx+1} no debe exceder 50 caracteres.")
    elif not is_update:
        data['ingredientes'] = []
    return errors

# --- Rutas para Ingredientes ---
@app.route('/api/ingredientes', methods=['GET'])
def get_all_ingredientes():
    try:
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                sql = "SELECT * FROM SGIC_INGREDIENTES ORDER BY nombre"
                cursor.execute(sql)
                ingredientes = [_row_to_dict(cursor, row) for row in cursor.fetchall()]
                return jsonify(convert_data_types(ingredientes))
    except Exception as e:
        app.logger.error(f"Error obteniendo ingredientes: {e}\n{traceback.format_exc() if DEBUG_MODE else ''}")
        return jsonify({"error": "Error interno del servidor", "detalle": str(e)}), 500

@app.route('/api/ingredientes/<int:ingrediente_id>', methods=['GET'])
def get_ingrediente_by_id(ingrediente_id):
    try:
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                sql = "SELECT * FROM SGIC_INGREDIENTES WHERE id_ingrediente = :id"
                cursor.execute(sql, {"id": ingrediente_id})
                ingrediente = _row_to_dict(cursor, cursor.fetchone())
                if not ingrediente: return jsonify({"error": "Ingrediente no encontrado"}), 404
                return jsonify(convert_data_types(ingrediente))
    except Exception as e:
        app.logger.error(f"Error obteniendo ingrediente {ingrediente_id}: {e}\n{traceback.format_exc() if DEBUG_MODE else ''}")
        return jsonify({"error": "Error interno del servidor", "detalle": str(e)}), 500

@app.route('/api/ingredientes', methods=['POST'])
def create_ingrediente():
    try:
        data = request.get_json()
        if not data: return jsonify({"error": "No se enviaron datos."}), 400
        errors = validate_ingrediente_data(data)
        if errors: return jsonify({"error": "Datos inválidos.", "detalles": errors}), 400

        insert_data = {
            'nombre': data['nombre'].strip(),
            'descripcion': data.get('descripcion', '').strip() or None,
            'unidad_medida': data['unidad_medida'].strip(),
            'stock_actual': float(Decimal(str(data.get('stock_actual', '0.000')))), # Convertir a float para BD
            'stock_minimo': float(Decimal(str(data.get('stock_minimo', '0.000'))))
        }
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                sql = """
                    INSERT INTO SGIC_INGREDIENTES (nombre, descripcion, unidad_medida, stock_actual, stock_minimo)
                    VALUES (:nombre, :descripcion, :unidad_medida, :stock_actual, :stock_minimo)
                    RETURNING id_ingrediente INTO :new_id
                """
                new_id_var = cursor.var(oracledb.NUMBER)
                cursor.execute(sql, {**insert_data, 'new_id': new_id_var})
                new_id = new_id_var.getvalue()[0]
                connection.commit()

                cursor.execute("SELECT * FROM SGIC_INGREDIENTES WHERE id_ingrediente = :id", {"id": new_id})
                ingrediente_creado = _row_to_dict(cursor, cursor.fetchone())
                return jsonify(convert_data_types(ingrediente_creado)), 201
    except oracledb.IntegrityError as e:
        error_obj, = e.args
        app.logger.warning(f"Error de integridad al crear ingrediente: {error_obj.message}")
        if error_obj.code == 1:
            return jsonify({"error": "El nombre del ingrediente ya existe."}), 409
        return jsonify({"error": "Error de integridad en la base de datos.", "detalle": error_obj.message}), 400
    except Exception as e:
        app.logger.error(f"Error creando ingrediente: {e}\n{traceback.format_exc() if DEBUG_MODE else ''}")
        return jsonify({"error": "Error interno del servidor.", "detalle": str(e)}), 500

@app.route('/api/ingredientes/<int:ingrediente_id>', methods=['PUT'])
def update_ingrediente(ingrediente_id):
    try:
        data = request.get_json()
        if not data: return jsonify({"error": "No se enviaron datos para actualizar."}), 400
        errors = validate_ingrediente_data(data, is_update=True)
        if errors: return jsonify({"error": "Datos inválidos para actualizar.", "detalles": errors}), 400

        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM SGIC_INGREDIENTES WHERE id_ingrediente = :id", {"id": ingrediente_id})
                if cursor.fetchone()[0] == 0: return jsonify({"error": "Ingrediente no encontrado."}), 404

                update_fields = []
                update_params = {"id": ingrediente_id}

                if 'nombre' in data:
                    update_fields.append("nombre = :nombre")
                    update_params['nombre'] = data['nombre'].strip()
                if 'descripcion' in data:
                    update_fields.append("descripcion = :descripcion")
                    update_params['descripcion'] = data.get('descripcion', '').strip() or None
                if 'unidad_medida' in data:
                    update_fields.append("unidad_medida = :unidad_medida")
                    update_params['unidad_medida'] = data['unidad_medida'].strip()
                if 'stock_actual' in data and data['stock_actual'] is not None:
                    update_fields.append("stock_actual = :stock_actual")
                    update_params['stock_actual'] = float(Decimal(str(data['stock_actual'])))
                if 'stock_minimo' in data and data['stock_minimo'] is not None:
                    update_fields.append("stock_minimo = :stock_minimo")
                    update_params['stock_minimo'] = float(Decimal(str(data['stock_minimo'])))

                if not update_fields: return jsonify({"mensaje": "No hay campos para actualizar.", "ingrediente_id": ingrediente_id}), 200

                sql = f"UPDATE SGIC_INGREDIENTES SET {', '.join(update_fields)}, fecha_actualizacion = CURRENT_TIMESTAMP WHERE id_ingrediente = :id"
                cursor.execute(sql, update_params)
                connection.commit()

                cursor.execute("SELECT * FROM SGIC_INGREDIENTES WHERE id_ingrediente = :id", {"id": ingrediente_id})
                ingrediente_actualizado = _row_to_dict(cursor, cursor.fetchone())
                return jsonify(convert_data_types(ingrediente_actualizado))
    except oracledb.IntegrityError as e:
        error_obj, = e.args
        app.logger.warning(f"Error de integridad al actualizar ingrediente {ingrediente_id}: {error_obj.message}")
        if error_obj.code == 1: return jsonify({"error": "El nombre del ingrediente ya existe para otro registro."}), 409
        return jsonify({"error": "Error de integridad en la base de datos.", "detalle": error_obj.message}), 400
    except Exception as e:
        app.logger.error(f"Error actualizando ingrediente {ingrediente_id}: {e}\n{traceback.format_exc() if DEBUG_MODE else ''}")
        return jsonify({"error": "Error interno del servidor.", "detalle": str(e)}), 500

@app.route('/api/ingredientes/<int:ingrediente_id>', methods=['DELETE'])
def delete_ingrediente(ingrediente_id):
    try:
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM SGIC_PRODUCTO_INGREDIENTES WHERE id_ingrediente_fk = :id", {"id": ingrediente_id})
                if cursor.fetchone()[0] > 0:
                    return jsonify({"error": "No se puede eliminar: el ingrediente está siendo usado en uno o más productos."}), 409

                cursor.execute("DELETE FROM SGIC_INGREDIENTES WHERE id_ingrediente = :id", {"id": ingrediente_id})
                if cursor.rowcount == 0: return jsonify({"error": "Ingrediente no encontrado para eliminar."}), 404
                connection.commit()
                return jsonify({"mensaje": "Ingrediente eliminado correctamente."})
    except oracledb.IntegrityError as e:
        error_obj, = e.args
        app.logger.warning(f"Error de integridad al eliminar ingrediente {ingrediente_id}: {error_obj.message}")
        return jsonify({"error": "Error de integridad al eliminar, posiblemente aún en uso en movimientos de inventario u otra tabla.", "detalle": error_obj.message}), 409
    except Exception as e:
        app.logger.error(f"Error eliminando ingrediente {ingrediente_id}: {e}\n{traceback.format_exc() if DEBUG_MODE else ''}")
        return jsonify({"error": "Error interno del servidor.", "detalle": str(e)}), 500

# --- Rutas para Productos ---
@app.route('/api/productos', methods=['GET'])
def get_all_productos():
    try:
        with get_db_connection() as connection:
            productos_completos = []
            with connection.cursor() as cursor_prod:
                cursor_prod.execute("SELECT * FROM SGIC_PRODUCTOS ORDER BY nombre")
                productos_base = [_row_to_dict(cursor_prod, row) for row in cursor_prod.fetchall()]

            if not productos_base: return jsonify([])

            for prod_dict_base in productos_base:
                prod_dict = convert_data_types(prod_dict_base.copy())
                with connection.cursor() as cursor_ing:
                    cursor_ing.execute("""
                        SELECT pi.id_ingrediente_fk, i.nombre as nombre_ingrediente, pi.cantidad_necesaria, pi.unidad_medida_receta
                        FROM SGIC_PRODUCTO_INGREDIENTES pi
                        JOIN SGIC_INGREDIENTES i ON pi.id_ingrediente_fk = i.id_ingrediente
                        WHERE pi.id_producto_fk = :id_prod ORDER BY i.nombre
                    """, {'id_prod': prod_dict["id_producto"]})

                    ingredientes_receta = []
                    for ing_row_tuple in cursor_ing:
                        ing_dict_raw = _row_to_dict(cursor_ing, ing_row_tuple)
                        if ing_dict_raw:
                            ingredientes_receta.append({
                                "id_ingrediente": ing_dict_raw["id_ingrediente_fk"],
                                "nombre_ingrediente": ing_dict_raw["nombre_ingrediente"],
                                "cantidad_necesaria": ing_dict_raw["cantidad_necesaria"], # Ya es Decimal o float
                                "unidad_medida_receta": ing_dict_raw["unidad_medida_receta"]
                            })
                    prod_dict["ingredientes"] = convert_data_types(ingredientes_receta)
                productos_completos.append(prod_dict)
            return jsonify(productos_completos)
    except Exception as e:
        app.logger.error(f"Error obteniendo productos: {e}\n{traceback.format_exc() if DEBUG_MODE else ''}")
        return jsonify({"error": "Error interno del servidor al obtener productos.", "detalle": str(e)}), 500

@app.route('/api/productos/<int:producto_id>', methods=['GET'])
def get_producto_by_id(producto_id):
    try:
        with get_db_connection() as connection:
            producto_dict = None
            with connection.cursor() as cursor:
                cursor.execute("SELECT * FROM SGIC_PRODUCTOS WHERE id_producto = :id", {"id": producto_id})
                producto_dict_base = _row_to_dict(cursor, cursor.fetchone())

            if not producto_dict_base: return jsonify({"error": "Producto no encontrado."}), 404

            producto_dict = convert_data_types(producto_dict_base.copy())

            with connection.cursor() as cursor_ing:
                cursor_ing.execute("""
                    SELECT pi.id_ingrediente_fk, i.nombre as nombre_ingrediente, pi.cantidad_necesaria, pi.unidad_medida_receta
                    FROM SGIC_PRODUCTO_INGREDIENTES pi JOIN SGIC_INGREDIENTES i ON pi.id_ingrediente_fk = i.id_ingrediente
                    WHERE pi.id_producto_fk = :id_prod ORDER BY i.nombre
                """, {'id_prod': producto_id})
                ingredientes_receta = []
                for ing_row_tuple in cursor_ing:
                    ing_dict_raw = _row_to_dict(cursor_ing, ing_row_tuple)
                    if ing_dict_raw:
                        ingredientes_receta.append({
                           "id_ingrediente": ing_dict_raw["id_ingrediente_fk"],
                           "nombre_ingrediente": ing_dict_raw["nombre_ingrediente"],
                           "cantidad_necesaria": ing_dict_raw["cantidad_necesaria"],
                           "unidad_medida_receta": ing_dict_raw["unidad_medida_receta"]
                        })
                producto_dict["ingredientes"] = convert_data_types(ingredientes_receta)
            return jsonify(producto_dict)
    except Exception as e:
        app.logger.error(f"Error obteniendo producto {producto_id}: {e}\n{traceback.format_exc() if DEBUG_MODE else ''}")
        return jsonify({"error": "Error interno del servidor.", "detalle": str(e)}), 500

@app.route('/api/productos', methods=['POST'])
def create_producto():
    try:
        data = request.get_json()
        if not data: return jsonify({"error": "No se enviaron datos."}), 400
        errors = validate_producto_data(data)
        if errors: return jsonify({"error": "Datos de producto inválidos.", "detalles": errors}), 400

        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                sql_prod = """
                    INSERT INTO SGIC_PRODUCTOS (nombre, descripcion, precio_venta, categoria)
                    VALUES (:nombre, :descripcion, :precio_venta, :categoria) RETURNING id_producto INTO :new_prod_id
                """
                new_prod_id_var = cursor.var(oracledb.NUMBER)
                cursor.execute(sql_prod, {
                    'nombre': data['nombre'].strip(),
                    'descripcion': data.get('descripcion', '').strip() or None,
                    'precio_venta': float(Decimal(str(data['precio_venta']))),
                    'categoria': data.get('categoria', '').strip() or None,
                    'new_prod_id': new_prod_id_var
                })
                new_producto_id = new_prod_id_var.getvalue()[0]

                if data.get('ingredientes'):
                    for item_receta in data['ingredientes']:
                        cursor.execute("SELECT COUNT(*) FROM SGIC_INGREDIENTES WHERE id_ingrediente = :id_ing",
                                       {"id_ing": int(item_receta["id_ingrediente"])})
                        if cursor.fetchone()[0] == 0:
                            connection.rollback()
                            return jsonify({"error": f"El ingrediente con ID {item_receta['id_ingrediente']} no existe."}), 400
                        cursor.execute("""
                            INSERT INTO SGIC_PRODUCTO_INGREDIENTES (id_producto_fk, id_ingrediente_fk, cantidad_necesaria, unidad_medida_receta)
                            VALUES (:id_prod, :id_ing, :cant, :unidad)
                        """, {
                            "id_prod": new_producto_id,
                            "id_ing": int(item_receta["id_ingrediente"]),
                            "cant": float(Decimal(str(item_receta["cantidad_necesaria"]))),
                            "unidad": item_receta["unidad_medida_receta"].strip()
                        })
                connection.commit()

                # Releer para devolver el producto completo
                cursor.execute("SELECT * FROM SGIC_PRODUCTOS WHERE id_producto = :id", {"id": new_producto_id})
                producto_creado_base = _row_to_dict(cursor, cursor.fetchone())
                # ... (código para re-leer ingredientes de receta y añadir a producto_creado) ...
                return jsonify(convert_data_types(producto_creado_base)), 201 # Simplificado, idealmente devolver con receta
    except oracledb.IntegrityError as e:
        error_obj, = e.args
        app.logger.warning(f"Error de integridad al crear producto: {error_obj.message}")
        if error_obj.code == 1: return jsonify({"error": "El nombre del producto ya existe."}), 409
        return jsonify({"error": "Error de integridad en la base de datos.", "detalle": error_obj.message}), 400
    except ValueError as ve:
        app.logger.warning(f"Error de valor al crear producto: {ve}")
        return jsonify({"error": "Datos inválidos.", "detalle": str(ve)}), 400
    except Exception as e:
        app.logger.error(f"Error creando producto: {e}\n{traceback.format_exc() if DEBUG_MODE else ''}")
        return jsonify({"error": "Error interno del servidor.", "detalle": str(e)}), 500

@app.route('/api/productos/<int:producto_id>', methods=['PUT'])
def update_producto(producto_id):
    try:
        data = request.get_json()
        if not data: return jsonify({"error": "No se enviaron datos para actualizar."}), 400
        errors = validate_producto_data(data, is_update=True)
        if errors: return jsonify({"error": "Datos de producto inválidos para actualizar.", "detalles": errors}), 400

        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM SGIC_PRODUCTOS WHERE id_producto = :id", {"id": producto_id})
                if cursor.fetchone()[0] == 0: return jsonify({"error": "Producto no encontrado."}), 404

                update_fields_prod = []
                update_params_prod = {"id": producto_id}
                # ... (construcción de update_fields_prod y update_params_prod) ...
                if 'nombre' in data: update_fields_prod.append("nombre = :nombre"); update_params_prod['nombre'] = data['nombre'].strip()
                # ... (otros campos) ...
                if 'precio_venta' in data and data['precio_venta'] is not None: update_fields_prod.append("precio_venta = :precio_venta"); update_params_prod['precio_venta'] = float(Decimal(str(data['precio_venta'])))


                if update_fields_prod:
                    sql_update_prod = f"UPDATE SGIC_PRODUCTOS SET {', '.join(update_fields_prod)}, fecha_actualizacion = CURRENT_TIMESTAMP WHERE id_producto = :id"
                    cursor.execute(sql_update_prod, update_params_prod)

                if 'ingredientes' in data:
                    cursor.execute("DELETE FROM SGIC_PRODUCTO_INGREDIENTES WHERE id_producto_fk = :id_prod", {"id_prod": producto_id})
                    for item_receta in data['ingredientes']:
                        # ... (verificación de existencia de ingrediente y INSERT) ...
                        cursor.execute("SELECT COUNT(*) FROM SGIC_INGREDIENTES WHERE id_ingrediente = :id_ing", {"id_ing": int(item_receta["id_ingrediente"])})
                        if cursor.fetchone()[0] == 0: connection.rollback(); return jsonify({"error": f"Ingrediente ID {item_receta['id_ingrediente']} no existe."}), 400
                        cursor.execute("INSERT INTO SGIC_PRODUCTO_INGREDIENTES VALUES (:id_prod, :id_ing, :cant, :unidad)", {
                            "id_prod": producto_id, "id_ing": int(item_receta["id_ingrediente"]),
                            "cant": float(Decimal(str(item_receta["cantidad_necesaria"]))), "unidad": item_receta["unidad_medida_receta"].strip()
                        })
                connection.commit()
                # ... (releer producto actualizado y devolver) ...
                return jsonify({"mensaje": "Producto actualizado"}), 200 # Simplificado
    except Exception as e:
        app.logger.error(f"Error actualizando producto {producto_id}: {e}\n{traceback.format_exc() if DEBUG_MODE else ''}")
        return jsonify({"error": "Error interno del servidor.", "detalle": str(e)}), 500

@app.route('/api/productos/<int:producto_id>', methods=['DELETE'])
def delete_producto(producto_id):
    try:
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM SGIC_PRODUCTOS WHERE id_producto = :id", {"id": producto_id})
                if cursor.fetchone()[0] == 0: return jsonify({"error": "Producto no encontrado para eliminar."}), 404
                # ON DELETE CASCADE se encarga de SGIC_PRODUCTO_INGREDIENTES
                # ON DELETE RESTRICT en SGIC_VENTAS previene borrar si hay ventas. Considerar lógica para esto.
                cursor.execute("SELECT COUNT(*) FROM SGIC_VENTAS WHERE id_producto_fk = :id", {"id": producto_id})
                if cursor.fetchone()[0] > 0:
                    return jsonify({"error": "No se puede eliminar: el producto tiene ventas registradas."}), 409

                cursor.execute("DELETE FROM SGIC_PRODUCTOS WHERE id_producto = :id", {"id": producto_id})
                if cursor.rowcount == 0: return jsonify({"error": "Producto no encontrado durante intento de eliminación."}), 404
                connection.commit()
                return jsonify({"mensaje": f"Producto ID {producto_id} eliminado correctamente."})
    except oracledb.IntegrityError as e: # Podría ser por otras FKs no manejadas
        error_obj, = e.args
        app.logger.warning(f"Error de integridad al eliminar producto {producto_id}: {error_obj.message}")
        return jsonify({"error": "Error de integridad al eliminar el producto.", "detalle": error_obj.message}), 409
    except Exception as e:
        app.logger.error(f"Error eliminando producto {producto_id}: {e}\n{traceback.format_exc() if DEBUG_MODE else ''}")
        return jsonify({"error": "Error interno del servidor.", "detalle": str(e)}), 500


# --- Rutas para Movimientos de Inventario ---
def _actualizar_stock_ingrediente_db(id_ingrediente, cantidad_ajuste_decimal, tipo_movimiento, observacion, cursor, id_venta=None):
    cursor.execute("SELECT stock_actual, nombre FROM SGIC_INGREDIENTES WHERE id_ingrediente = :id FOR UPDATE", {'id': id_ingrediente})
    ingrediente_row = cursor.fetchone()
    if not ingrediente_row: raise ValueError(f"Ingrediente ID {id_ingrediente} no encontrado.")

    stock_actual_decimal = Decimal(str(ingrediente_row[0]))
    nombre_ingrediente = ingrediente_row[1]

    nuevo_stock_decimal = stock_actual_decimal + cantidad_ajuste_decimal
    nuevo_stock_decimal = nuevo_stock_decimal.quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)

    if nuevo_stock_decimal < 0:
        raise ValueError(f"Stock insuficiente para '{nombre_ingrediente}' (ID {id_ingrediente}). Requiere {abs(cantidad_ajuste_decimal)}, disponible {stock_actual_decimal}.")

    cursor.execute("UPDATE SGIC_INGREDIENTES SET stock_actual = :nuevo_stock WHERE id_ingrediente = :id",
                   {'nuevo_stock': float(nuevo_stock_decimal), 'id': id_ingrediente})

    cursor.execute("""
        INSERT INTO SGIC_MOVIMIENTOS_INVENTARIO
        (id_ingrediente_fk, id_venta_fk, tipo_movimiento, cantidad, fecha_movimiento, observacion)
        VALUES (:id_ing, :id_venta, :tipo, :cant, :fecha, :obs)
    """, {
        "id_ing": id_ingrediente, "id_venta": id_venta, "tipo": tipo_movimiento,
        "cant": abs(float(cantidad_ajuste_decimal)), "fecha": datetime.now(), "obs": observacion
    })
    app.logger.info(f"Movimiento '{tipo_movimiento}' de {abs(cantidad_ajuste_decimal)} para '{nombre_ingrediente}' (Venta ID: {id_venta}). Nuevo stock: {nuevo_stock_decimal}")

@app.route('/api/inventario/entrada', methods=['POST'])
def registrar_entrada_inventario():
    data = request.get_json()
    if not data or not all(k in data for k in ('id_ingrediente', 'cantidad')):
        return jsonify({"error": "Datos incompletos. Se requiere id_ingrediente y cantidad."}), 400
    try:
        id_ingrediente = int(data['id_ingrediente'])
        cantidad = Decimal(str(data['cantidad']))
        if cantidad <= 0: return jsonify({"error": "La cantidad debe ser positiva."}), 400
    except (ValueError, TypeError):
        return jsonify({"error": "id_ingrediente y cantidad deben ser números válidos."}), 400

    tipo_movimiento = data.get('tipo_movimiento', "ENTRADA_COMPRA").strip().upper()
    observacion = data.get('observacion', f"Entrada de {cantidad} unidades.")
    try:
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                _actualizar_stock_ingrediente_db(id_ingrediente, cantidad, tipo_movimiento, observacion, cursor)
                connection.commit()

        with get_db_connection() as conn_read:
             with conn_read.cursor() as cursor_read:
                cursor_read.execute("SELECT * FROM SGIC_INGREDIENTES WHERE id_ingrediente = :id", {"id": id_ingrediente})
                ing_actualizado = _row_to_dict(cursor_read, cursor_read.fetchone())
        return jsonify({"mensaje": "Entrada registrada y stock actualizado.", "ingrediente_actualizado": convert_data_types(ing_actualizado)})
    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400
    except Exception as e:
        app.logger.error(f"Error registrando entrada: {e}\n{traceback.format_exc() if DEBUG_MODE else ''}")
        return jsonify({"error": "Error interno del servidor", "detalle": str(e)}), 500

@app.route('/api/inventario/salida_venta_producto', methods=['POST'])
def registrar_salida_por_venta():
    data = request.get_json()
    if not data or 'id_producto' not in data or 'cantidad_vendida' not in data:
        return jsonify({"error": "Se requiere id_producto y cantidad_vendida."}), 400

    try:
        id_producto = int(data['id_producto'])
        cantidad_productos_vendidos = int(data['cantidad_vendida'])
        if cantidad_productos_vendidos <= 0:
            return jsonify({"error": "La cantidad vendida debe ser positiva."}), 400
    except ValueError:
        return jsonify({"error": "id_producto y cantidad_vendida deben ser números válidos."}), 400

    new_venta_id = None
    try:
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT nombre, precio_venta FROM SGIC_PRODUCTOS WHERE id_producto = :id", {'id': id_producto})
                producto_info = cursor.fetchone()
                if not producto_info:
                    raise ValueError(f"Producto ID {id_producto} no encontrado.")
                nombre_producto, precio_unitario_actual_db = producto_info[0], Decimal(str(producto_info[1]))

                monto_total_venta = precio_unitario_actual_db * Decimal(str(cantidad_productos_vendidos))

                sql_insert_venta = """
                    INSERT INTO SGIC_VENTAS (id_producto_fk, cantidad_vendida, precio_unitario_venta, monto_total_venta, fecha_venta, observacion)
                    VALUES (:id_prod, :cant_vend, :precio_u, :monto_t, :fecha, :obs)
                    RETURNING id_venta INTO :new_id_venta
                """
                new_id_venta_var = cursor.var(oracledb.NUMBER)
                cursor.execute(sql_insert_venta, {
                    "id_prod": id_producto, "cant_vend": cantidad_productos_vendidos,
                    "precio_u": float(precio_unitario_actual_db), "monto_t": float(monto_total_venta),
                    "fecha": datetime.now(), "obs": data.get("observacion_venta", f"Venta de {cantidad_productos_vendidos} x {nombre_producto}"),
                    "new_id_venta": new_id_venta_var
                })
                new_venta_id = new_id_venta_var.getvalue()[0]

                cursor.execute("SELECT id_ingrediente_fk, cantidad_necesaria FROM SGIC_PRODUCTO_INGREDIENTES WHERE id_producto_fk = :id_prod", {'id_prod': id_producto})
                ingredientes_receta = cursor.fetchall()

                if not ingredientes_receta and cantidad_productos_vendidos > 0 :
                    app.logger.info(f"Venta de '{nombre_producto}' (Venta ID: {new_venta_id}) no afecta stock (sin receta).")
                else:
                    for id_ing, cant_necesaria_unidad_db in ingredientes_receta:
                        cant_necesaria_unidad = Decimal(str(cant_necesaria_unidad_db))
                        cantidad_total_a_descontar = cant_necesaria_unidad * Decimal(str(cantidad_productos_vendidos))
                        observ_mov = f"Salida por Venta ID: {new_venta_id} ({cantidad_productos_vendidos} x '{nombre_producto}')"
                        _actualizar_stock_ingrediente_db(id_ing, -cantidad_total_a_descontar, "SALIDA_VENTA", observ_mov, cursor, id_venta=new_venta_id)
                connection.commit()

        return jsonify({
            "mensaje": f"{cantidad_productos_vendidos} x '{nombre_producto}' vendida(s) (Venta ID: {new_venta_id}). Stock actualizado.",
            "id_venta": new_venta_id
        })
    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400
    except Exception as e:
        app.logger.error(f"Error registrando salida por venta: {e}\n{traceback.format_exc() if DEBUG_MODE else ''}")
        return jsonify({"error": "Error interno del servidor", "detalle": str(e)}), 500

# --- Rutas para Reportes ---
@app.route('/api/reportes/existencias', methods=['GET'])
def reporte_existencias():
    try:
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                sql = """
                    SELECT id_ingrediente, nombre, unidad_medida, stock_actual, stock_minimo,
                           CASE WHEN stock_actual < stock_minimo THEN 'BAJO STOCK' ELSE 'OK' END as estado_stock
                    FROM SGIC_INGREDIENTES ORDER BY nombre
                """
                cursor.execute(sql)
                existencias = [_row_to_dict(cursor, row) for row in cursor.fetchall()]
                return jsonify(convert_data_types(existencias))
    except Exception as e:
        app.logger.error(f"Error reporte existencias: {e}\n{traceback.format_exc() if DEBUG_MODE else ''}")
        return jsonify({"error": "Error interno", "detalle": str(e)}), 500

@app.route('/api/reportes/historial_movimientos', methods=['GET'])
def reporte_historial_movimientos():
    fecha_inicio_str = request.args.get('fecha_inicio')
    fecha_fin_str = request.args.get('fecha_fin')
    params = {}
    sql_conditions = []
    try:
        if fecha_inicio_str:
            params['fecha_inicio'] = datetime.strptime(fecha_inicio_str, '%Y-%m-%d').date()
            sql_conditions.append("TRUNC(mi.fecha_movimiento) >= :fecha_inicio")
        if fecha_fin_str:
            params['fecha_fin'] = datetime.strptime(fecha_fin_str, '%Y-%m-%d').date()
            sql_conditions.append("TRUNC(mi.fecha_movimiento) <= :fecha_fin")
    except ValueError:
        return jsonify({"error": "Formato de fecha inválido. Usar YYYY-MM-DD."}), 400

    try:
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                sql_base = """
                    SELECT mi.id_movimiento, mi.fecha_movimiento, mi.tipo_movimiento,
                           i.nombre as nombre_ingrediente, mi.cantidad, i.unidad_medida,
                           mi.id_venta_fk,
                           p_venta.nombre as nombre_producto_venta,
                           mi.observacion
                    FROM SGIC_MOVIMIENTOS_INVENTARIO mi
                    JOIN SGIC_INGREDIENTES i ON mi.id_ingrediente_fk = i.id_ingrediente
                    LEFT JOIN SGIC_VENTAS v ON mi.id_venta_fk = v.id_venta
                    LEFT JOIN SGIC_PRODUCTOS p_venta ON v.id_producto_fk = p_venta.id_producto
                """
                if sql_conditions: sql_base += " WHERE " + " AND ".join(sql_conditions)
                sql_base += " ORDER BY mi.fecha_movimiento DESC, mi.id_movimiento DESC"

                cursor.execute(sql_base, params)
                movimientos = [_row_to_dict(cursor, row) for row in cursor.fetchall()]
                return jsonify(convert_data_types(movimientos))
    except Exception as e:
        app.logger.error(f"Error historial movimientos: {e}\n{traceback.format_exc() if DEBUG_MODE else ''}")
        return jsonify({"error": "Error interno", "detalle": str(e)}), 500

@app.route('/api/reportes/productos_ranking_ventas', methods=['GET'])
def reporte_productos_ranking_ventas():
    orden = request.args.get('orden', 'desc').lower()
    limite = request.args.get('limite', default=10, type=int)
    if orden not in ['asc', 'desc']:
        return jsonify({"error": "Parámetro 'orden' debe ser 'asc' o 'desc'."}), 400

    try:
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                sql = f"""
                    SELECT
                        p.id_producto, p.nombre as nombre_producto,
                        SUM(v.cantidad_vendida) as total_unidades_vendidas,
                        SUM(v.monto_total_venta) as total_monto_ventas
                    FROM SGIC_VENTAS v
                    JOIN SGIC_PRODUCTOS p ON v.id_producto_fk = p.id_producto
                    GROUP BY p.id_producto, p.nombre
                    ORDER BY total_unidades_vendidas { 'ASC' if orden == 'asc' else 'DESC' }, total_monto_ventas { 'ASC' if orden == 'asc' else 'DESC' }
                    FETCH FIRST :limite ROWS ONLY
                """
                cursor.execute(sql, {'limite': limite})
                productos = [_row_to_dict(cursor, row) for row in cursor.fetchall()]
                return jsonify(convert_data_types(productos))
    except Exception as e:
        app.logger.error(f"Error ranking productos: {e}\n{traceback.format_exc() if DEBUG_MODE else ''}")
        return jsonify({"error": "Error interno", "detalle": str(e)}), 500

@app.route('/api/reportes/promedio_ventas_diarias', methods=['GET'])
def reporte_promedio_ventas_diarias():
    try:
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                sql = """
                    SELECT
                        TRUNC(fecha_venta) as dia_venta,
                        SUM(monto_total_venta) as total_venta_dia,
                        COUNT(DISTINCT id_venta) as numero_transacciones_dia
                    FROM SGIC_VENTAS
                    GROUP BY TRUNC(fecha_venta)
                    ORDER BY dia_venta DESC
                """
                cursor.execute(sql)
                ventas_diarias_detalle = [_row_to_dict(cursor, row) for row in cursor.fetchall()]

                promedio_general_diario = 0
                if ventas_diarias_detalle:
                    total_monto_todos_los_dias = sum(Decimal(str(d['total_venta_dia'])) for d in ventas_diarias_detalle)
                    numero_dias_con_ventas = len(ventas_diarias_detalle)
                    if numero_dias_con_ventas > 0:
                        promedio_general_diario = total_monto_todos_los_dias / Decimal(str(numero_dias_con_ventas))

                return jsonify({
                    "detalle_por_dia": convert_data_types(ventas_diarias_detalle),
                    "promedio_general_monto_diario": float(promedio_general_diario)
                })
    except Exception as e:
        app.logger.error(f"Error promedio ventas diarias: {e}\n{traceback.format_exc() if DEBUG_MODE else ''}")
        return jsonify({"error": "Error interno", "detalle": str(e)}), 500

@app.route('/api/reportes/promedio_ventas_mensuales', methods=['GET'])
def reporte_promedio_ventas_mensuales():
    try:
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                sql = """
                    SELECT
                        TO_CHAR(fecha_venta, 'YYYY-MM') as mes_venta,
                        SUM(monto_total_venta) as total_venta_mes,
                        COUNT(DISTINCT id_venta) as numero_transacciones_mes
                    FROM SGIC_VENTAS
                    GROUP BY TO_CHAR(fecha_venta, 'YYYY-MM')
                    ORDER BY mes_venta DESC
                """
                cursor.execute(sql)
                ventas_mensuales_detalle = [_row_to_dict(cursor, row) for row in cursor.fetchall()]

                promedio_general_mensual = 0
                if ventas_mensuales_detalle:
                    total_monto_todos_los_meses = sum(Decimal(str(m['total_venta_mes'])) for m in ventas_mensuales_detalle)
                    numero_meses_con_ventas = len(ventas_mensuales_detalle)
                    if numero_meses_con_ventas > 0:
                        promedio_general_mensual = total_monto_todos_los_meses / Decimal(str(numero_meses_con_ventas))

                return jsonify({
                    "detalle_por_mes": convert_data_types(ventas_mensuales_detalle),
                    "promedio_general_monto_mensual": float(promedio_general_mensual)
                })
    except Exception as e:
        app.logger.error(f"Error promedio ventas mensuales: {e}\n{traceback.format_exc() if DEBUG_MODE else ''}")
        return jsonify({"error": "Error interno", "detalle": str(e)}), 500

# --- Error Handlers y __main__ ---
@app.errorhandler(404)
def not_found_error(error): return jsonify({"error": "Endpoint no encontrado"}), 404
@app.errorhandler(405)
def method_not_allowed_error(error): return jsonify({"error": "Método no permitido"}), 405
@app.errorhandler(500)
def internal_server_error_handler(error):
    original_exception = getattr(error, 'original_exception', error)
    app.logger.error(f"Error 500: {original_exception}\n{traceback.format_exc() if DEBUG_MODE else ''}")
    return jsonify({"error": "Error interno del servidor (manejador general)"}), 500

if __name__ == '__main__':
    app.logger.info("--------------------------------------------------")
    app.logger.info(f"Iniciando el servidor Flask SGIC (app.run) para __name__: {__name__}")
    if config_valid:
        if pool:
            app.logger.info(f"✅ Aplicación lista - Pool de conexiones disponible.")
        else:
            app.logger.warning(f"⚠️  POOL DE CONEXIONES NO DISPONIBLE. Error: {pool_error}")
    else:
        app.logger.critical(f"⚠️  CONFIGURACIÓN INVÁLIDA: {config_msg}. Operaciones de DB fallarán.")

    host = os.environ.get('FLASK_HOST', '0.0.0.0')
    port = int(os.environ.get('FLASK_PORT', '5000'))
    run_debug_mode = DEBUG_MODE

    app.logger.info(f"Iniciando servidor en http://{host}:{port}/ (Flask debug={run_debug_mode})")
    app.run(host=host, port=port, debug=run_debug_mode)

