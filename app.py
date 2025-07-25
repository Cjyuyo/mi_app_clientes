from flask import Flask, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, or_
import os
import uuid
from datetime import datetime

app = Flask(__name__)

# --- Configuración de la Base de Datos PostgreSQL ---
DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://lientes_db_prod_user:FzmjghqgD9UPN3I3Ex3Q8KpLlgFDvUDI@dpg-d1vomdadbo4c73fnv9sg-a/lientes_db_prod')
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL.replace("://", "ql://", 1) if "://" in DATABASE_URL else DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.secret_key = 'una_llave_muy_segura_debe_ir_aqui'

db = SQLAlchemy(app)

# --- Modelos de la Base de Datos (El nuevo "schema") ---
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
    tasa_dia = db.Column(db.Float)
    monto_bs = db.Column(db.Float)
    pago_en = db.Column(db.String(50))
    forma_pago = db.Column(db.String(50))
    banco = db.Column(db.String(100))
    referencia = db.Column(db.String(100))
    lugar_emision = db.Column(db.String(100))
    cantidad_en_letras = db.Column(db.String(255))
    por_concepto_de = db.Column(db.String(255))
    estado = db.Column(db.String(50))
    recibo = db.Column(db.String(50))
    fecha_pago = db.Column(db.DateTime, default=datetime.utcnow)

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
        flash('Cliente registrado con éxito.', 'success')
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
        for key, value in request.form.items():
            if hasattr(cliente, key):
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
        try:
            monto_usd = float(request.form.get('monto_usd', 0))
            tasa_dia = float(request.form.get('tasa_dia', 0) or 0)
            monto_bs = float(request.form.get('monto_bs', 0) or 0)
        except (ValueError, TypeError):
            flash('Los montos y tasas deben ser números válidos.', 'error')
            return render_template('registrar_pago.html', cliente=cliente)

        nuevo_pago = Pago(
            cliente_id=cliente.id, monto_usd=monto_usd, tasa_dia=tasa_dia, monto_bs=monto_bs,
            pago_en=request.form.get('pago_en'), forma_pago=request.form.get('forma_pago'),
            banco=request.form.get('banco'), referencia=request.form.get('referencia'),
            lugar_emision=request.form.get('lugar_emision'), cantidad_en_letras=request.form.get('cantidad_en_letras'),
            por_concepto_de=request.form.get('por_concepto_de'), estado=request.form.get('estado'),
            recibo=request.form.get('recibo')
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

@app.route('/recibo/<id_cliente>/<id_pago>')
def ver_recibo(id_cliente, id_pago):
    cliente = Cliente.query.get_or_404(id_cliente)
    pago = Pago.query.get_or_404(id_pago)
    if pago.cliente_id != cliente.id:
        flash('Acceso no autorizado al recibo.', 'error')
        return redirect(url_for('consulta_clientes'))
    return render_template('recibo.html', pago=pago, cliente=cliente)

if __name__ == '__main__':
    app.run(debug=True)
