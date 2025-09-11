-- === Base de egresos planificados (extensión mínima para semanal) ===
CREATE TABLE IF NOT EXISTS egresos_planificados (
  id SERIAL PRIMARY KEY,
  tipo TEXT CHECK (tipo IN ('Fijo','Variable','Devolucion')) NOT NULL DEFAULT 'Fijo',
  titulo TEXT NOT NULL,
  descripcion TEXT,
  monto_base_usd NUMERIC(12,2) NOT NULL,
  metodo_referencia TEXT CHECK (metodo_referencia IN ('USD_EFECTIVO','VES_BCV','VES_EUR','VES_BINANCE','USDT')) NOT NULL DEFAULT 'USD_EFECTIVO',
  frecuencia TEXT CHECK (frecuencia IN ('Semanal','Quincenal','Mensual','Anual','Unico')) NOT NULL DEFAULT 'Mensual',
  -- Semanal:
  intervalo_semana INT NOT NULL DEFAULT 1,           -- cada N semanas
  byday TEXT,                                        -- 'MO,TU,WE,TH,FR,SA,SU'
  -- Mensual/Quincenal (v1 simple):
  dia_mes INT,                                       -- 1–31
  dias_quincena TEXT,                                -- '1,16'
  -- Rango:
  fecha_inicio_recurrencia DATE NOT NULL DEFAULT CURRENT_DATE,
  fecha_fin_recurrencia DATE,
  estado TEXT CHECK (estado IN ('activo','pausado')) NOT NULL DEFAULT 'activo',
  created_by INT,
  created_at TIMESTAMP NOT NULL DEFAULT NOW(),
  updated_by INT,
  updated_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_egp_estado ON egresos_planificados (estado);
CREATE INDEX IF NOT EXISTS idx_egp_frecuencia ON egresos_planificados (frecuencia);

-- === Ocurrencias por período (semanal / mensual) ===
CREATE TABLE IF NOT EXISTS egresos_ocurrencias (
  id SERIAL PRIMARY KEY,
  egreso_id INT NOT NULL REFERENCES egresos_planificados(id) ON DELETE CASCADE,
  fecha_programada DATE NOT NULL,
  periodo_tipo TEXT CHECK (periodo_tipo IN ('mensual','semanal')) NOT NULL,
  periodo_clave TEXT NOT NULL,                       -- 'YYYY-MM' o 'YYYY-Www'
  monto_programado_usd NUMERIC(12,2) NOT NULL,
  monto_pagado_usd NUMERIC(12,2) NOT NULL DEFAULT 0,
  estado TEXT CHECK (estado IN ('pendiente','parcial','pagado')) NOT NULL DEFAULT 'pendiente',
  UNIQUE (egreso_id, periodo_tipo, periodo_clave, fecha_programada)
);

CREATE INDEX IF NOT EXISTS idx_eocc_periodo ON egresos_ocurrencias (periodo_tipo, periodo_clave);
CREATE INDEX IF NOT EXISTS idx_eocc_estado ON egresos_ocurrencias (estado);

-- === Pagos aplicados a ocurrencias (conciliados con tesorería) ===
CREATE TABLE IF NOT EXISTS egresos_pagos (
  id SERIAL PRIMARY KEY,
  egreso_ocurrencia_id INT NOT NULL REFERENCES egresos_ocurrencias(id) ON DELETE CASCADE,
  movimiento_tesoreria_id INT,                       -- FK suave si tu tabla existe
  monto_original NUMERIC(14,2) NOT NULL,
  moneda TEXT CHECK (moneda IN ('USD','VES','USDT')) NOT NULL,
  tasa_aplicada NUMERIC(14,6),
  monto_equivalente_usd NUMERIC(14,2) NOT NULL,
  fecha_pago TIMESTAMP NOT NULL DEFAULT NOW(),
  nota TEXT
);

CREATE INDEX IF NOT EXISTS idx_epagos_occ ON egresos_pagos (egreso_ocurrencia_id);
CREATE INDEX IF NOT EXISTS idx_epagos_fecha ON egresos_pagos (fecha_pago);

-- === Campos de referencia en movimientos de tesorería (si aplica) ===
ALTER TABLE movimientos_tesoreria
  ADD COLUMN IF NOT EXISTS referencia_tipo TEXT,     -- 'EGRESO'
  ADD COLUMN IF NOT EXISTS referencia_id INT;        -- egreso_ocurrencia_id

CREATE INDEX IF NOT EXISTS idx_mt_ref ON movimientos_tesoreria (referencia_tipo, referencia_id);
