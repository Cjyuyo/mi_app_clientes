from flask import Flask, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, or_
import os
import uuid
from datetime import datetime

app = Flask(__name__)

# --- Configuración de la Base de Datos PostgreSQL ---
# Usamos la URL que siempre ha funcionado.
# La corrección .replace("://", "ql://", 1) es un arreglo común para Heroku/Render.
DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://lientes_db_prod_user:FzmjghqgD9UPN3I3Ex3Q8KpLlgFDvUDI@dpg-d1vomdadbo4c73fnv9sg-a/lientes_db_prod')
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL.replace("://", "ql://", 1)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.secret_key = 'tu_llave_secreta_aqui_es_muy_importante'

db = SQLAlchemy(app)

# --- Modelos de la Base de Datos (El nuevo "schema") ---
# Esto define la estructura de tus tablas directamente en el código.
class Cliente(db.Model):
    __tablename__ = 'clientes'
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    nombre_apellido = db.Column(db.String(120), nullable=False)
    cedula = db.Column(db.String(20), unique=True, nullable=False)
    contrato_nro = db.Column(db.String(50))
    telefono = db.Column(db.String(20))
    asesor = db.Column(db.String(100))
    responsable = db.Column(db.String(100))
    fecha_ingreso = db.Column(db.String(20))
    grupo = db.Column(db.String(50))
    bien_solicitado = db.Column(db.String(100))
    plan_contratado = db.Column(db.String(50))
    cuotas_totales = db.Column(db.Integer)
    moneda_pago = db.Column(db.String(10))
    valor_cuota = db.Column(db.Float)
    inscripcion_monto = db.Column(db.Float)
    proceso = db.Column(db.String(50), default='INSCRITO')
    estatus = db.Column(db.String(50), default='ACTIVO')
    pagos = db.relationship('Pago', backref='cliente', lazy=True, cascade="all, delete-orphan")

class Pago(db.Model):
    __tablename__ = 'pagos'
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    cliente_id = db.Column(db.String(36), db.ForeignKey('clientes.id'), nullable=False)
    monto_usd = db.Column(db.Float)
    forma_pago = db.Column(db.String(50))
    recibo = db.Column(db.String(50))
    fecha_pago = db.Column(db.DateTime, default=datetime.utcnow)
    estado = db.Column(db.String(50))

# --- Creación de las tablas ---
# Esto asegura que las tablas existan en tu base de datos.
with app.app_context():
    db.create_all()

def calcular_estado_de_cuenta(cliente):
    valor_cuota = cliente.valor_cuota or 0
    cuotas_totales = cliente.cuotas_totales or 1
    total_pagado = sum(p.monto_usd for p in cliente.pagos if p.monto_usd)
    cuotas_progresivas = 0
    balance_a_favor = 0.0
    progreso_progresivas = 0.0
    progreso_balance = 0.0
    if valor_cuota > 0 and cuotas_totales > 0:
        cuotas_pagadas_float = total_pagado / valor_cuota
        cuotas_progresivas = int(cuotas_pagadas_float)
        balance_a_favor = (cuotas_pagadas_float - cuotas_progresivas) * valor_cuota
        progreso_progresivas = (cuotas_progresivas / cuotas_totales) * 100
        progreso_balance = (balance_a_favor / valor_cuota) * 100 if valor_cuota > 0 else 0
    inscripcion_monto = cliente.inscripcion_monto or 0
    valor_cancelado = inscripcion_monto + total_pagado
    cuotas_pendientes = cuotas_totales - cuotas_progresivas
    return {
        "cuotas_progresivas": cuotas_progresivas, "balance_a_favor": balance_a_favor,
        "progreso_progresivas": min(progreso_progresivas, 100), "progreso_balance": min(progreso_balance, 100),
        "valor_cancelado": valor_cancelado, "cuotas_totales": cuotas_totales, "cuotas_pendientes": cuotas_pendientes
    }

# --- Rutas de la Aplicación ---
@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        nuevo_cliente = Cliente(
            nombre_apellido=request.form.get('nombre_apellido'), cedula=request.form.get('cedula'),
            contrato_nro=request.form.get('contrato_nro'), telefono=request.form.get('telefono'),
            asesor=request.form.get('asesor'), responsable=request.form.get('responsable'),
            fecha_ingreso=request.form.get('fecha_ingreso'), grupo=request.form.get('grupo'),
            bien_solicitado=request.form.get('bien_solicitado'), plan_contratado=request.form.get('plan_contratado'),
            cuotas_totales=int(request.form.get('cuotas_totales', 0)), moneda_pago=request.form.get('moneda_pago'),
            valor_cuota=float(request.form.get('valor_cuota', 0)), inscripcion_monto=float(request.form.get('inscripcion_monto', 0)),
        )
        db.session.add(nuevo_cliente)
        db.session.commit()
        flash('Cliente registrado con éxito en la base de datos.', 'success')
        return redirect(url_for('consulta_clientes'))
    return render_template('index.html')

@app.route('/consulta', methods=['GET', 'POST'])
def consulta_clientes():
    if request.method == 'POST':
        termino = request.form.get('termino_busqueda', '').lower().strip()
        search_term = f"%{termino}%"
        resultados = Cliente.query.filter(or_(
            func.lower(Cliente.cedula).like(search_term),
            func.lower(Cliente.nombre_apellido).like(search_term),
            func.lower(Cliente.telefono).like(search_term)
        )).all()
        if len(resultados) == 1:
            return redirect(url_for('detalle_cliente', id_cliente=resultados[0].id))
        return render_template('consulta.html', clientes_encontrados=resultados)
    return render_template('consulta.html')

@app.route('/cliente/<id_cliente>')
def detalle_cliente(id_cliente):
    cliente = Cliente.query.get_or_404(id_cliente)
    estado_cuenta = calcular_estado_de_cuenta(cliente)
    return render_template('consulta.html', cliente=cliente, **estado_cuenta)

@app.route('/editar/<id_cliente>', methods=['GET', 'POST'])
def editar_cliente(id_cliente):
    cliente = Cliente.query.get_or_404(id_cliente)
    if request.method == 'POST':
        # Actualiza todos los campos del cliente desde el formulario
        for key, value in request.form.items():
            if hasattr(cliente, key):
                # Convierte a número si es necesario
                if key in ['cuotas_totales']:
                    setattr(cliente, key, int(value) if value else None)
                elif key in ['valor_cuota', 'inscripcion_monto']:
                    setattr(cliente, key, float(value) if value else None)
                else:
                    setattr(cliente, key, value)
        db.session.commit()
        flash('Cliente actualizado correctamente.', 'success')
        return redirect(url_for('detalle_cliente', id_cliente=cliente.id))
    return render_template('edit_cliente.html', cliente=cliente)

@app.route('/registrar_pago/<id_cliente>', methods=['GET', 'POST'])
def registrar_pago(id_cliente):
    cliente = Cliente.query.get_or_404(id_cliente)
    if request.method == 'POST':
        nuevo_pago = Pago(
            cliente_id=cliente.id,
            monto_usd=float(request.form.get('monto_usd', 0)),
            forma_pago=request.form.get('forma_pago'),
            recibo=request.form.get('recibo'),
            estado=request.form.get('estado')
        )
        db.session.add(nuevo_pago)
        db.session.commit()
        flash('Pago registrado con éxito.', 'success')
        return redirect(url_for('detalle_cliente', id_cliente=id_cliente))
    return render_template('registrar_pago.html', cliente=cliente)

@app.route('/eliminar/<id_cliente>')
def eliminar_cliente(id_cliente):
    cliente = Cliente.query.get_or_404(id_cliente)
    db.session.delete(cliente)
    db.session.commit()
    flash('Cliente eliminado exitosamente.', 'success')
    return redirect(url_for('consulta_clientes'))

if __name__ == '__main__':
    app.run(debug=True)
