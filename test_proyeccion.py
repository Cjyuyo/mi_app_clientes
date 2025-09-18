import pytest
from decimal import Decimal
from proyeccion import calcular_proyeccion


@pytest.fixture
def tasas_ok():
    return {
        "usd_bcv": 40.0,
        "eur_bcv": 44.0,
        "binance_actual": 40.5,
        "binance_prevision": 41.0,
        "venta_usd": 225.0,
        "recompra_usd": 247.0,
    }


@pytest.fixture
def parametros_ok():
    return {
        "colchon_pct": 0.20,
        "bloque_directo_keys": ["devoluciones_usd", "alquiler"],
        "pagan_por_225_247_keys": ["publicidad", "salud"],
    }


def test_estructura_basica(tasas_ok, parametros_ok):
    ingresos = {"solv_imp": {"efectivo": 1000, "euro": 0, "usdt": 200, "nequi": 50, "bs_sc": 4000, "bs_cc": 0}}
    egresos = {
        "divisas": {"alquiler": 500, "publicidad": 100},
        "bs_bcv": 50, "bs_euro_bcv": 20,
        "variables_bs": {"devoluciones_bs": 30, "registro": 10},
        "variables_usd": {"devoluciones_usd": 70},
    }
    out = calcular_proyeccion(ingresos, egresos, tasas_ok, parametros_ok)
    assert "escenarios" in out and "tasas_y_margenes" in out and "glosario" in out
    assert set(out["escenarios"].keys()) == {"full", "base", "base_mora"}
    for k in ("full", "base", "base_mora"):
        esc = out["escenarios"][k]
        for key in (
            "ingresos_por_bucket", "egresos_divisas_directo", "egresos_divisas_225_247",
            "egresos_bs_directo", "saldos", "colchon_total"
        ):
            assert key in esc


def test_penalidad_225_247_por_claves(tasas_ok, parametros_ok):
    ingresos = {"solv_imp": {"efectivo": 0, "usdt": 1000, "nequi": 0, "euro": 0, "bs_sc": 10000, "bs_cc": 0}}
    egresos = {"divisas": {"publicidad": 200}, "bs_bcv": 0, "bs_euro_bcv": 0, "variables_bs": {}, "variables_usd": {}}
    out = calcular_proyeccion(ingresos, egresos, tasas_ok, parametros_ok)
    esc = out["escenarios"]["base"]
    venta = Decimal(str(tasas_ok["venta_usd"]))
    recompra = Decimal(str(tasas_ok["recompra_usd"]))
    penalidad_esp = (recompra - venta) * Decimal("200")
    assert Decimal(esc["penalidad_225_247_usd_eq"]) == penalidad_esp.quantize(Decimal("0.01"))


def test_carril_por_item_override(tasas_ok, parametros_ok):
    ingresos = {"solv_imp": {"efectivo": 0, "usdt": 1000, "nequi": 0, "euro": 0, "bs_sc": 20000, "bs_cc": 0}}
    egresos = {
        "divisas": {},
        "carril_225_247_items": [
            {"concepto": "publicidad", "monto_usd": 100, "venta": 229.0, "recompra": 247.0},  # Δ=18
            {"concepto": "salud",      "monto_usd":  50, "venta": 230.0, "recompra": 246.0},  # Δ=16
        ],
        "bs_bcv": 0, "bs_euro_bcv": 0, "variables_bs": {}, "variables_usd": {}
    }
    out = calcular_proyeccion(ingresos, egresos, tasas_ok, parametros_ok)
    esc = out["escenarios"]["base"]
    assert Decimal(esc["penalidad_225_247_usd_eq"]) == Decimal("2600.00")  # 100*18 + 50*16


def test_colchon_se_aplica_al_final(tasas_ok, parametros_ok):
    ingresos = {"solv_imp": {"efectivo": 1000, "usdt": 0, "nequi": 0, "euro": 0, "bs_sc": 0, "bs_cc": 0}}
    egresos = {"divisas": {}, "bs_bcv": 0, "bs_euro_bcv": 0, "variables_bs": {}, "variables_usd": {}}
    out = calcular_proyeccion(ingresos, egresos, tasas_ok, parametros_ok)
    esc = out["escenarios"]["base"]
    neto = Decimal(esc["saldos"]["neto_usd_eq"])
    colchon = Decimal(esc["colchon_total"])
    liquido = Decimal(esc["saldos"]["liquido_usd_eq"])
    assert liquido == (neto - colchon).quantize(Decimal("0.01"))


def test_deficit_usd_cubierto_en_bs(tasas_ok, parametros_ok):
    ingresos = {"solv_imp": {"efectivo": 100, "usdt": 0, "nequi": 0, "euro": 0, "bs_sc": 4000, "bs_cc": 4000}}
    egresos = {"divisas": {"alquiler": 300}, "bs_bcv": 0, "bs_euro_bcv": 0, "variables_bs": {}, "variables_usd": {}}
    out = calcular_proyeccion(ingresos, egresos, tasas_ok, parametros_ok)
    esc = out["escenarios"]["base"]
    assert Decimal(esc["deficit_usd_cubierto_en_bs"]) == Decimal("200.00")
    assert Decimal(esc["saldos"]["liquido_usd_eq"]) <= Decimal(esc["saldos"]["neto_usd_eq"])


def test_validaciones_tasas():
    ingresos = {"solv_imp": {"efectivo": 0, "bs_sc": 1}}
    egresos = {"divisas": {}, "bs_bcv": 0, "bs_euro_bcv": 0, "variables_bs": {}, "variables_usd": {}}
    tasas_bad = {"usd_bcv": 0.0, "eur_bcv": 44.0, "binance_actual": 40.5, "binance_prevision": 41.0,
                 "venta_usd": 225.0, "recompra_usd": 247.0}
    with pytest.raises(ValueError):
        calcular_proyeccion(ingresos, egresos, tasas_bad, {"colchon_pct": 0.2, "bloque_directo_keys": [], "pagan_por_225_247_keys": []})

    tasas_bad2 = {"usd_bcv": 40.0, "eur_bcv": 44.0, "binance_actual": 40.5, "binance_prevision": 41.0,
                  "venta_usd": 250.0, "recompra_usd": 247.0}
    with pytest.raises(ValueError):
        calcular_proyeccion(ingresos, egresos, tasas_bad2, {"colchon_pct": 0.2, "bloque_directo_keys": [], "pagan_por_225_247_keys": []})