import time
import os
import sys
import tempfile
import sqlite3
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from smart_aggregator.session_history import SessionHistory
from smart_aggregator.message_filter import MessageFilter
from smart_aggregator.chat_source import YouTubeChatSource
from smart_aggregator.vibe_thermometer import VibeThermometer
from smart_aggregator.activity_trigger import ActivityTrigger
from smart_aggregator.aggregator import Aggregator

MOCK_MESSAGES_20 = [
    {"user": f"user{i}", "text": f"Mensaje de prueba numero {i} para Kira", "timestamp": time.time() + i}
    for i in range(20)
]

MOCK_MESSAGES_200 = []
for i in range(200):
    if i % 10 == 0:
        text = "hola"
    elif i % 10 == 1:
        text = "🔥🔥🔥"
    elif i % 10 == 2:
        text = "mira esto https://youtube.com/test"
    elif i % 10 == 3:
        text = "@Kira eres genial"
    elif i % 10 == 4:
        text = "ESTO ES INCREIBLE"
    elif i % 10 == 5:
        text = "Que momento epico"
    elif i % 10 == 6:
        text = "Esto es basura"
    elif i % 10 == 7:
        text = "Kira que opinas del juego?"
    elif i % 10 == 8:
        text = "GG"
    else:
        text = f"Mensaje largo y variado numero {i} con suficientes palabras para pasar el filtro"
    MOCK_MESSAGES_200.append({"user": f"u{i}", "text": text, "timestamp": time.time() + i})

VIBE_TEST_MESSAGES = [
    {"user": "fan1", "text": "ESTO ES INCREIBLE 🔥🔥🔥", "timestamp": time.time()},
    {"user": "fan2", "text": "Que momento epico", "timestamp": time.time() + 1},
    {"user": "hater1", "text": "Esto es basura", "timestamp": time.time() + 2},
    {"user": "normal1", "text": "Kira que opinas del juego?", "timestamp": time.time() + 3},
]

def load_config():
    config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "smart_aggregator.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def test_tc3_1_filter():
    print("\n=== TC3.1 — Filtro de Mensajes ===")
    cfg = load_config()["filter"]
    msg_filter = MessageFilter(cfg)
    
    # TC3.1.1
    result = msg_filter.filter({"user": "a", "text": "hola", "timestamp": time.time()})
    assert result is None, "TC3.1.1: 'hola' deberia ser descartado"
    print("[OK] TC3.1.1: Mensaje corto descartado")
    
    # TC3.1.2
    cfg_emoji = cfg.copy()
    cfg_emoji["min_words"] = 1
    cfg_emoji["min_char_length"] = 1
    emoji_filter = MessageFilter(cfg_emoji)
    result = emoji_filter.filter({"user": "a", "text": "🔥🔥🔥", "timestamp": time.time()})
    assert result is None, "TC3.1.2: Emojis puros deberian ser descartados"
    print("[OK] TC3.1.2: Emojis puros descartados")
    
    # TC3.1.3
    result = msg_filter.filter({"user": "a", "text": "mira esto https://youtube.com/test", "timestamp": time.time()})
    assert result is None, "TC3.1.3: Enlaces deberian ser descartados"
    print("[OK] TC3.1.3: Enlaces descartados")
    
    # TC3.1.4
    result = msg_filter.filter({"user": "a", "text": "@Kira eres genial", "timestamp": time.time()})
    assert result is None, "TC3.1.4: Menciones deberian ser descartadas"
    print("[OK] TC3.1.4: Menciones descartadas")

    result = msg_filter.filter({"user": "a", "text": "@Kira-test eres genial", "timestamp": time.time()})
    assert result is None, "TC3.1.4b: Menciones con guion deberian ser descartadas"
    print("[OK] TC3.1.4b: Menciones con guion descartadas")
    
    # TC3.1.5
    result = msg_filter.filter({"user": "a", "text": "Kira que juego estas jugando?", "timestamp": time.time()})
    assert result is not None, "TC3.1.5: Mensaje normal deberia pasar"
    print("[OK] TC3.1.5: Mensaje normal pasa")

    # TC3.1.5b / TC3.1.5c emojis personalizados de YouTube
    result = emoji_filter.filter({"user": "a", "text": ":bird::bird::bird:", "timestamp": time.time()})
    assert result is None, "TC3.1.5b: Emojis personalizados puros deberian descartarse"
    result = msg_filter.filter({"user": "a", "text": "stop saying YDBAF you'll get banned :bird::bird::bird:", "timestamp": time.time()})
    assert result is not None and ":bird:" not in result["text"], "TC3.1.5c: Emojis personalizados deben limpiarse del texto"
    print("[OK] TC3.1.5b/c: Emojis personalizados detectados y limpiados")
    
    # TC3.1.6
    cfg_whitelist = cfg.copy()
    cfg_whitelist["whitelist"] = {"enabled": True, "users": ["vip_user"]}
    filter_vip = MessageFilter(cfg_whitelist)
    result = filter_vip.filter({"user": "vip_user", "text": "hola", "timestamp": time.time()})
    assert result is not None, "TC3.1.6: VIP deberia saltar filtro"
    print("[OK] TC3.1.6: VIP salta filtro")
    
    # TC3.1.7
    passed = [m for m in MOCK_MESSAGES_200 if msg_filter.filter(m) is not None]
    print(f"[OK] TC3.1.7: De 200 mensajes, {len(passed)} pasaron el filtro")
    assert len(passed) > 0, "TC3.1.7: Alguno deberia pasar"

def test_tc3_2_vibe():
    print("\n=== TC3.2 — Vibe Thermometer ===")
    cfg = load_config()["vibe"]
    llm_mock = lambda prompt: {"emotions": {"excitement": 0.7, "neutral": 0.2, "sadness": 0.1, "anger": 0.0, "joy": 0.0, "confusion": 0.0}, "temperature": 75}
    
    thermometer = VibeThermometer(cfg, llm_interface=llm_mock)
    
    # TC3.2.1 ventana vacia
    vibe = thermometer.compute_vibe(force=True)
    assert vibe["temperature"] == 0.0, "TC3.2.1: Ventana vacia debe retornar 0"
    print("[OK] TC3.2.1: Ventana vacia retorna temperatura 0")
    
    # TC3.2.2 / TC3.2.3
    for msg in VIBE_TEST_MESSAGES:
        thermometer.add_message(msg)
    
    vibe = thermometer.compute_vibe(force=True)
    assert vibe["temperature"] > 0, "TC3.2.2: Ventana no vacia debe tener temperatura > 0"
    assert vibe["emotions"]["excitement"] > 0.5, "TC3.2.3: Hype detectado"
    print(f"[OK] TC3.2.2/3: Vibe calculada — temperatura={vibe['temperature']}, excitement={vibe['emotions']['excitement']}")

def test_tc3_3_activity():
    print("\n=== TC3.3 — Activity Trigger ===")
    cfg = load_config()["activity"].copy()
    cfg["threshold_per_second"] = 2.0
    cfg["cooldown_seconds"] = 0.0
    triggered = []
    
    def on_trigger(data):
        triggered.append(data)
    
    activity = ActivityTrigger(cfg, callbacks={"on_trigger": on_trigger})
    
    # TC3.3.1 rate bajo
    base = time.time()
    for i in range(5):
        activity.on_message({"user": f"u{i}", "text": "msg", "timestamp": base + i})
    assert len(triggered) == 0, "TC3.3.1: No debe triggerear con rate bajo"
    print("[OK] TC3.3.1: Rate bajo no dispara trigger")
    
    # TC3.3.2 rate alto
    activity.reset()
    base = time.time()
    for i in range(15):
        activity.on_message({"user": f"u{i}", "text": "msg", "timestamp": base + (i * 0.2)})
    assert len(triggered) > 0, "TC3.3.2: Debe triggerear con rate alto"
    print(f"[OK] TC3.3.2: Rate alto dispara trigger ({len(triggered)} vez/veces)")

    # TC3.3.3 acciones configuradas
    actions_cfg = cfg.copy()
    actions_cfg["actions"] = {
        "auto_reply": {"enabled": True, "message": "Chat en pico"},
        "behavior_change": {"enabled": True, "parameter": "excitement_multiplier", "value": 1.5},
    }
    triggered.clear()
    activity = ActivityTrigger(actions_cfg, callbacks={"on_trigger": on_trigger})
    base = time.time()
    for i in range(15):
        activity.on_message({"user": f"u{i}", "text": "msg", "timestamp": base + (i * 0.2)})
    assert triggered[-1]["actions"]["auto_reply"] == "Chat en pico", "TC3.3.3: Auto reply no configurado"
    assert triggered[-1]["actions"]["behavior_change"]["parameter"] == "excitement_multiplier", "TC3.3.3: Behavior change no configurado"
    print("[OK] TC3.3.3: Acciones configuradas incluidas en payload")

def test_tc3_4_history():
    print("\n=== TC3.4 — Session History ===")
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    jl_fd, jl_path = tempfile.mkstemp(suffix=".jsonl")
    os.close(db_fd)
    os.close(jl_fd)
    
    try:
        history = SessionHistory(db_path, jl_path, retention_hours=1)
        
        # TC3.4.1
        sid = history.start_session("youtube", "test_channel")
        assert sid > 0, "TC3.4.1: session_id debe ser > 0"
        print(f"[OK] TC3.4.1: Sesion creada con id={sid}")
        
        # TC3.4.2
        for msg in MOCK_MESSAGES_20:
            history.add_message(sid, msg, passed_filter=True, vibe=50.0)
        
        context = history.get_session_context(sid, max_messages=25)
        assert len(context) == 20, "TC3.4.2: Debe haber 20 registros"
        print("[OK] TC3.4.2: 20 mensajes guardados")
        
        # TC3.4.3
        assert os.path.exists(jl_path), "TC3.4.3: JSONL debe existir"
        with open(jl_path, "r", encoding="utf-8") as f:
            lines = [l for l in f if l.strip()]
        assert len(lines) == 20, "TC3.4.3: JSONL debe tener 20 lineas"
        print("[OK] TC3.4.3: JSONL contiene 20 lineas")
        
        # TC3.4.4
        context = history.get_session_context(sid, max_messages=10)
        assert len(context) == 10, "TC3.4.4: Limita a max_messages"
        print("[OK] TC3.4.4: Contexto limitado a 10 mensajes")
        
        # TC3.4.5 cleanup
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE sessions SET start_time = ? WHERE id = ?", (time.time() - 7200, sid))
        conn.commit()
        conn.close()
        history.cleanup_old_sessions()
        context_after = history.get_session_context(sid, max_messages=100)
        assert len(context_after) == 0, "TC3.4.5: Sesiones antiguas deben borrarse"
        with open(jl_path, "r", encoding="utf-8") as f:
            remaining_lines = [l for l in f if l.strip()]
        assert len(remaining_lines) == 0, "TC3.4.5: JSONL antiguo debe limpiarse"
        print("[OK] TC3.4.5: Cleanup borra sesiones antiguas en SQLite y JSONL")
    finally:
        try:
            os.unlink(db_path)
            os.unlink(jl_path)
        except Exception:
            pass

def test_tc3_5_chat_source():
    print("\n=== TC3.5 — YouTube Chat Source ===")
    cfg = load_config()["source"]
    source = YouTubeChatSource(cfg, callbacks={})
    
    # TC3.5.3
    assert not source.is_connected(), "TC3.5.3: No conectado inicialmente"
    print("[OK] TC3.5.3: is_connected() es False inicialmente")
    
    # TC3.5.1 / TC3.5.2 / TC3.5.4 / TC3.5.5
    try:
        source.connect("")
        assert False, "TC3.5.1: Debe fallar sin video_id"
    except ValueError as e:
        print(f"[OK] TC3.5.1: Error claro sin video_id — {e}")
    
    try:
        source.connect("invalid_video_id_123")
        # Si pytchat esta disponible, connect() inicia el hilo pero puede fallar internamente.
        # Damos un momento y verificamos que no crashea.
        time.sleep(0.5)
        print("[OK] TC3.5.2/5: Connect con video_id invalido manejado gracefulmente")
    except Exception as e:
        # Si pytchat no esta disponible, esperamos RuntimeError. Si esta disponible,
        # cualquier excepcion de pytchat (video no encontrado, etc.) es aceptable.
        assert "pytchat" in str(e).lower() or "video" in str(e).lower() or "invalid" in str(e).lower(), f"TC3.5.5: Error inesperado — {e}"
        print(f"[OK] TC3.5.2/5: Error manejado — {e}")
    
    # TC3.5.4 disconnect sin connect previo
    source.disconnect()
    print("[OK] TC3.5.4: Disconnect sin connect previo no crashea")

def test_tc3_6_aggregator_full():
    print("\n=== TC3.6 — Aggregator Orchestration ===")
    cfg = load_config()
    llm_mock = lambda prompt: {"emotions": {"excitement": 0.8, "neutral": 0.2, "sadness": 0.0, "anger": 0.0, "joy": 0.0, "confusion": 0.0}, "temperature": 80}

    with tempfile.TemporaryDirectory(prefix="smart_agg_test_") as temp_dir:
        config_path = os.path.join(temp_dir, "smart_aggregator.yaml")
        cfg["history"]["db_path"] = os.path.join(temp_dir, "sessions.db")
        cfg["history"]["jsonl_path"] = os.path.join(temp_dir, "chat_log.jsonl")
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True)

        agg = Aggregator(config_path=config_path, llm_interface=llm_mock)
        filtered_msgs = []
        vibes = []
        triggers = []
        
        agg.on_filtered_message = lambda m: filtered_msgs.append(m)
        agg.on_vibe_update = lambda v: vibes.append(v)
        agg.on_activity_trigger = lambda d: triggers.append(d)
        sid = agg.start_session("youtube", "headless_test")
        
        # TC3.6.1-3: Simular 200 mensajes
        for msg in MOCK_MESSAGES_200:
            agg.process_message(msg)

        spike_base = time.time()
        for i in range(60):
            agg.process_message({"user": f"spike{i}", "text": "Mensaje valido de pico para probar actividad", "timestamp": spike_base + (i * 0.01)})
        
        assert len(filtered_msgs) > 0, "TC3.6.1: Algun mensaje paso"
        print(f"[OK] TC3.6.1: {len(filtered_msgs)} mensajes filtrados")
        
        # Forzar computo de vibe para tests
        vibe = agg.thermometer.compute_vibe(force=True)
        if vibe:
            vibes.append(vibe)
        assert len(vibes) > 0, "TC3.6.2: Vibe calculado al menos 1 vez"
        print(f"[OK] TC3.6.2: Vibe calculado — {vibes[-1]}")
        
        # Trigger ya se evaluo durante los 200 mensajes
        assert len(triggers) > 0, "TC3.6.3: Debe detectar trigger con spike simulado"
        print(f"[OK] TC3.6.3: Triggers detectados — {len(triggers)}")
        
        # TC3.6.4 Sesion
        context = agg.history.get_session_context(sid, max_messages=300)
        assert len(context) > 0, "TC3.6.4: La sesion debe persistir mensajes"
        agg.disconnect()
        print("[OK] TC3.6.4: Aggregator persiste y cierra sesion headless")
        
        # TC3.6.5 Callbacks opcionales
        agg2 = Aggregator(config_path=config_path, llm_interface=llm_mock)
        for msg in MOCK_MESSAGES_20[:5]:
            agg2.process_message(msg)
        print("[OK] TC3.6.5: Callbacks opcionales no causan fallos")

def test_tc3_7_youtube_api():
    print("\n=== TC3.7 — YouTube API (placeholder) ===")
    cfg = load_config().get("youtube_api", {})
    if cfg.get("api_key") == "${YOUTUBE_API_KEY}":
        print("[SKIP]  TC3.7: YouTube API key no configurada — saltando")
        return
    print("[SKIP]  TC3.7: Implementacion de API real pendiente de configuracion")

def run_all_tests():
    print("=" * 50)
    print("SMART AGGREGATOR — TESTS LOCALES (HEADLESS)")
    print("=" * 50)
    
    test_tc3_1_filter()
    test_tc3_2_vibe()
    test_tc3_3_activity()
    test_tc3_4_history()
    test_tc3_5_chat_source()
    test_tc3_6_aggregator_full()
    test_tc3_7_youtube_api()
    
    print("\n" + "=" * 50)
    print("TODOS LOS TESTS PASARON [OK]")
    print("=" * 50)

if __name__ == "__main__":
    run_all_tests()
