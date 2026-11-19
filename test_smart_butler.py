# -*- coding: utf-8 -*-
"""
Bộ test offline tổng hợp cho Smart Butler AI & Smart Home Hub.
Chạy hoàn toàn trên SQLite tạm (không gọi mạng Gemini / Telegram thật).
"""
import ast
import datetime
import json
import os
import pathlib
import re
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

ROOT_DIR = pathlib.Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

_TMP_DIR = tempfile.TemporaryDirectory(prefix="smart_butler_test_")
os.environ["DB_PATH"] = os.path.join(_TMP_DIR.name, "test_butler.db")
os.environ["BACKUP_DIR"] = os.path.join(_TMP_DIR.name, "backups")
os.environ["DASHBOARD_PIN"] = "246810"
os.environ["GEMINI_API_KEY"] = "test-key-offline"
os.environ["GEMINI_API_KEYS"] = "test-key-offline"
os.environ["BOT_TOKEN"] = "123456789:AAFakeTokenForOfflineUnitTestsOnly0000"

if "aiogram" not in sys.modules:
    try:
        import aiogram  # noqa: F401
    except ImportError:
        fake_aiogram = types.ModuleType("aiogram")
        fake_aiogram.Bot = lambda *a, **kw: MagicMock()
        fake_dispatcher_cls = type(
            "Dispatcher",
            (),
            {"message": lambda self, *a, **kw: (lambda fn: fn)},
        )
        fake_aiogram.Dispatcher = fake_dispatcher_cls
        fake_aiogram.F = MagicMock()
        fake_filters = types.ModuleType("aiogram.filters")
        fake_filters.Command = lambda *a, **kw: ("cmd", a)
        fake_types = types.ModuleType("aiogram.types")
        fake_types.Message = object
        fake_types.BufferedInputFile = lambda data, filename="": (data, filename)
        fake_types.FSInputFile = lambda path: path
        fake_enums = types.ModuleType("aiogram.enums")
        fake_enums.ParseMode = type("ParseMode", (), {"MARKDOWN": "Markdown"})
        fake_exc = types.ModuleType("aiogram.exceptions")
        fake_exc.TelegramBadRequest = type("TelegramBadRequest", (Exception,), {})
        fake_aiogram.filters = fake_filters
        fake_aiogram.types = fake_types
        fake_aiogram.enums = fake_enums
        fake_aiogram.exceptions = fake_exc
        sys.modules["aiogram"] = fake_aiogram
        sys.modules["aiogram.filters"] = fake_filters
        sys.modules["aiogram.types"] = fake_types
        sys.modules["aiogram.enums"] = fake_enums
        sys.modules["aiogram.exceptions"] = fake_exc

import config
import database
import persona
import circadian
import diary_helper
import character_bible
import sampling_helper
import trend_helper
import smart_plugs
import fan_controller
import lan_presence
import router_controller
import esp32_audio
import mcp_tools
import habit_engine
import proactive_engine
import gemini_client
import web_dashboard
import bot as butler_bot
from aiohttp.test_utils import TestClient, TestServer


class SmartButlerBaseTest(unittest.TestCase):
    def setUp(self):
        self._db_file = os.path.join(_TMP_DIR.name, f"{self.id().split('.')[-1]}.db")
        if os.path.exists(self._db_file):
            os.remove(self._db_file)
        config.DB_PATH = self._db_file
        database.init_db()
        persona.save_persona_config({
            "butler_name": "Atlas",
            "owner_name": "Chủ nhà",
            "self_pronoun": "",
            "style": "butler",
            "weather_city": "Ho_Chi_Minh_City",
            "ai_bio": "",
            "ai_personality": "",
            "speaking_style": "",
            "owner_bio": "",
            "custom_notes": "",
        })


class TestPrivacyAndCodeDiscipline(SmartButlerBaseTest):
    def test_zero_personal_leaks_across_repo(self):
        forbidden = re.compile(
            r"PhatNT|13092005|CuuHo|12345678|192\.168\.3\.|34:ea:34|a4:cf:12|8115636519|130905|"
            r"Linh Đan|Linh Dan|Đà Lạt|Da Lat|mèo Bơ|dating_sim|photo_catalog|dalat_kb|"
            r"morning_dream|night_dream|người yêu|yêu xa|bạn gái",
            re.IGNORECASE,
        )
        skip_dirs = {".git", "venv", ".venv", "__pycache__", "node_modules", "music_cache", "backups"}
        hits = []
        for path in sorted(ROOT_DIR.rglob("*")):
            if not path.is_file():
                continue
            rel_parts = set(path.relative_to(ROOT_DIR).parts[:-1])
            if rel_parts & skip_dirs:
                continue
            if path.name == os.path.basename(__file__):
                continue
            if path.suffix not in (".py", ".html", ".js", ".c", ".cpp", ".h", ".ino", ".ini", ".md", ".example", ".service", ".bat"):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for idx, line in enumerate(text.splitlines(), start=1):
                if forbidden.search(line):
                    hits.append(f"{path.relative_to(ROOT_DIR)}:{idx}: {line.strip()[:80]}")
        self.assertEqual(hits, [], f"Phát hiện rò rỉ dữ liệu riêng tư:\n" + "\n".join(hits))

    def test_database_ast_connections_use_open_conn(self):
        db_src = (ROOT_DIR / "database.py").read_text(encoding="utf-8")
        tree = ast.parse(db_src)
        raw_connect_funcs = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for child in ast.walk(node):
                    if (
                        isinstance(child, ast.Call)
                        and isinstance(child.func, ast.Name)
                        and child.func.id == "get_connection"
                        and node.name != "_open_conn"
                    ):
                        raw_connect_funcs.append(node.name)
        self.assertEqual(raw_connect_funcs, [], f"Hàm gọi trực tiếp get_connection ngoài _open_conn: {raw_connect_funcs}")


class TestPersonaAndPrompts(SmartButlerBaseTest):
    def test_persona_save_and_prompt_injection(self):
        saved = persona.save_persona_config({
            "butler_name": "Jarvis",
            "owner_name": "Minh",
            "self_pronoun": "tôi",
            "style": "assistant",
            "weather_city": "Hanoi",
            "ai_bio": "Trợ lý kỹ thuật 25 tuổi am hiểu Smart Home.",
            "ai_personality": "Thẳng thắn, tinh tế và đáng tin cậy.",
            "speaking_style": "Gãy gọn 1-2 câu, tránh lặp ý.",
            "owner_bio": "Kỹ sư hệ thống thích nhạc Instrumental.",
            "custom_notes": "Luôn báo ngắn gọn 2 câu.",
        })
        self.assertEqual(saved["butler_name"], "Jarvis")
        self.assertEqual(saved["owner_name"], "Minh")
        self.assertEqual(saved["self_pronoun"], "tôi")
        self.assertEqual(saved["self_ref"], "tôi")
        self.assertEqual(saved["style"], "assistant")
        self.assertEqual(saved["weather_city"], "Hanoi")
        self.assertIn("25 tuổi", saved["prompt_preview"])

        p1 = character_bible.build_pass1_system_instruction(
            affinity_score=80,
            mood="sẵn sàng",
            known_facts=[],
            daily_trends="",
            daily_diary="",
        )
        p2 = character_bible.build_pass2_system_instruction()
        p3 = character_bible.build_pass3_logic_audit_instruction()
        pe = character_bible.build_proactive_eval_instruction(
            affinity_score=80,
            mood="sẵn sàng",
            personality_traits={},
            time_context={"date_str": "2026-11-18", "time_str": "19:30", "weekday_vi": "Thứ Tư", "session_key": "evening", "session_label": "Buổi tối"},
            current_activity="Trực phòng",
            daily_trends="",
        )
        for txt in (p1, p2, p3, pe):
            self.assertIn("Jarvis", txt)
            self.assertIn("Minh", txt)
        for txt in (p1, p2, pe):
            self.assertIn("Trợ lý kỹ thuật 25 tuổi am hiểu Smart Home", txt)
            self.assertIn("Thẳng thắn, tinh tế và đáng tin cậy", txt)
            self.assertIn("Gãy gọn 1-2 câu, tránh lặp ý", txt)
            self.assertIn("Kỹ sư hệ thống thích nhạc Instrumental", txt)
        self.assertIn("Hanoi", p1)
        self.assertIn("Luôn báo ngắn gọn 2 câu", p1)
        self.assertIn("Luôn báo ngắn gọn 2 câu", p2)
        self.assertIn("fetch_tools", p1)
        self.assertIn("habit_action", p1)


class TestSmartPlugsAndFanAndSpeaker(SmartButlerBaseTest):
    def test_plugs_empty_default_seed_and_matching(self):
        self.assertEqual(smart_plugs.DEFAULT_SEED_PLUGS, [])
        self.assertEqual(smart_plugs.load_plugs_config(), [])

        smart_plugs.save_plugs_config([
            {"mac": "aa:bb:cc:dd:ee:01", "ip": "192.168.1.51", "name": "Đèn phòng", "aliases": ["đèn", "đèn phòng"]},
            {"mac": "aa:bb:cc:dd:ee:02", "ip": "192.168.1.52", "name": "Quạt thông gió", "aliases": ["quạt thông gió", "thông gió"]},
        ])
        plugs = smart_plugs.load_plugs_config()
        self.assertEqual(len(plugs), 2)

        cmd_lamp = smart_plugs.detect_natural_plug_command("tắt đèn phòng giúp mình", plugs=plugs)
        self.assertIsNotNone(cmd_lamp)
        self.assertEqual(cmd_lamp["action"], "power_off")
        self.assertEqual(cmd_lamp["targets"], ["aa:bb:cc:dd:ee:01"])

        cmd_exh = smart_plugs.detect_natural_plug_command("bật quạt thông gió lên", plugs=plugs)
        self.assertIsNotNone(cmd_exh)
        self.assertEqual(cmd_exh["action"], "power_on")
        self.assertEqual(cmd_exh["targets"], ["aa:bb:cc:dd:ee:02"])

    def test_fan_controller_snapshot_shape(self):
        ctrl = fan_controller.get_controller()
        snap = ctrl.get_status_snapshot()
        self.assertIn("configured", snap)
        self.assertIn("online", snap)
        self.assertIn("state", snap)

    def test_proactive_engine_without_dalat_or_dream(self):
        uid = 99001
        database.get_or_create_user(uid)
        gate = proactive_engine.evaluate_wakeup_gate(uid, now_ts=1794990000.0)
        self.assertIn("allow", gate)
        self.assertFalse(gate.get("has_unshared_dream", False))
        coupon = proactive_engine.peek_wake_lottery_coupon(uid, now_ts=1794990000.0)
        self.assertIsNotNone(coupon)
        self.assertIn(coupon["category"], ("habit_check", "weather_tip", "mini_question", "music_vibe", "open_loop_ping"))
        snap = proactive_engine.get_engine_snapshot(uid, now_ts=1794990000.0)
        self.assertNotIn("morning_dream", snap)


class TestMcpToolsAndJsonRpc(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._db_file = os.path.join(_TMP_DIR.name, f"{self.id().split('.')[-1]}.db")
        if os.path.exists(self._db_file):
            os.remove(self._db_file)
        config.DB_PATH = self._db_file
        database.init_db()

    async def test_mcp_tools_list_and_speaker_control(self):
        tools = mcp_tools.list_mcp_tools()
        names = {t["name"] for t in tools}
        self.assertEqual(len(tools), 13)
        for expected in (
            "get_fan_state", "get_plugs_state", "get_presence_state",
            "get_room_climate", "get_speaker_state", "get_room_overview",
            "get_learned_habits", "get_router_state",
            "set_fan", "set_plug", "control_speaker", "manage_learned_habit", "control_router",
        ):
            self.assertIn(expected, names)

        fake_snap = {
            "status": "playing",
            "volume": 35,
            "auto_dj": True,
            "voice_autoplay": False,
            "ducking_voice": False,
            "current_track": {"title": "Lo-fi Focus", "uploader": "Chilled", "duration_sec": 180},
            "queue_len": 2,
            "eq": {"preset": "warm", "enabled": True},
            "normalize": {"enabled": True, "effective_db": -1.5},
            "esp32": {"online": True, "ip": "192.168.1.88"},
            "dht11": {"temperature_c": 26.5, "humidity_pct": 65.0},
            "alarms": [],
            "session": {},
        }
        fake_mgr = MagicMock()
        fake_mgr.get_status_snapshot.return_value = fake_snap
        with patch("esp32_audio.get_manager", return_value=fake_mgr), \
             patch("esp32_audio.execute_ai_music_action", new=AsyncMock(return_value="Đã chỉnh âm lượng 45%.")):
            res_spk = await mcp_tools.call_mcp_tool("get_speaker_state", {})
            self.assertEqual(res_spk["playback_state"], "playing")
            self.assertTrue(res_spk["is_playing"])
            self.assertEqual(res_spk["volume"], 35)
            self.assertEqual(res_spk["track_title"], "Lo-fi Focus")

            ctrl_res = await mcp_tools.call_mcp_tool("control_speaker", {"action": "volume", "volume": 45})
            self.assertTrue(ctrl_res["ok"])
            self.assertIn("45%", ctrl_res["note"])

    async def test_mcp_jsonrpc_protocol_and_http_auth(self):
        resp_init, code_init, _ = await mcp_tools.handle_mcp_jsonrpc_payload({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26"},
        })
        self.assertEqual(code_init, 200)
        self.assertEqual(resp_init["result"]["serverInfo"]["name"], mcp_tools.MCP_SERVER_NAME)

        resp_unk, _, _ = await mcp_tools.handle_mcp_jsonrpc_payload({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "no_such_tool", "arguments": {}},
        })
        self.assertEqual(resp_unk["error"]["code"], -32602)

        app = web_dashboard.create_app(bot=None, target_user_id=1001)
        async with TestClient(TestServer(app)) as client:
            proxied_unauth = await client.post(
                "/mcp",
                headers={"X-Forwarded-For": "8.8.8.8"},
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            )
            self.assertEqual(proxied_unauth.status, 401)

            pin = web_dashboard.get_effective_pin()
            auth_ok = await client.post(
                "/mcp",
                headers={"X-Forwarded-For": "8.8.8.8", "Authorization": f"Bearer {pin}"},
                json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
            )
            self.assertEqual(auth_ok.status, 200)
            body = await auth_ok.json()
            self.assertEqual(len(body["result"]["tools"]), 13)


class TestHabitEngine(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._db_file = os.path.join(_TMP_DIR.name, f"{self.id().split('.')[-1]}.db")
        if os.path.exists(self._db_file):
            os.remove(self._db_file)
        config.DB_PATH = self._db_file
        database.init_db()

    def test_habit_mining_ai_rule_and_negative_feedback(self):
        # Giả lập bật Đèn phòng khoảng 18:10 liên tục trong 4 ngày
        vn_tz = datetime.timezone(datetime.timedelta(hours=7))
        base_day = datetime.datetime(2026, 11, 10, 18, 10, tzinfo=vn_tz)
        for day_i in range(4):
            ts = (base_day + datetime.timedelta(days=day_i, minutes=day_i * 3)).timestamp()
            habit_engine.record_device_event(
                "plug", "Đèn phòng", "power_on", source="user_telegram", now_ts=ts
            )

        learned = habit_engine.analyze_and_sync_habits(now_ts=(base_day + datetime.timedelta(days=5)).timestamp())
        self.assertTrue(len(learned) >= 1)
        lamp_habits = [h for h in learned if h["target"] == "Đèn phòng" and h["action"] == "power_on"]
        self.assertEqual(len(lamp_habits), 1)
        h = lamp_habits[0]
        self.assertEqual(h["status"], "active")
        self.assertGreaterEqual(float(h["confidence"]), 0.70)

        # Kiểm tra negative feedback khi vừa tự động chạy (habit_auto) mà user tắt ngược lại trong 2 phút
        hk = h["habit_key"]
        run_ts = (base_day + datetime.timedelta(days=6, minutes=0)).timestamp()
        habit_engine.record_device_event(
            "plug", "Đèn phòng", "power_on", source="habit_auto", now_ts=run_ts
        )
        database.update_habit_runtime(hk, last_triggered_at=run_ts, last_triggered_date="2026-11-16", now_ts=run_ts)
        old_conf = float(database.get_learned_habit(hk)["confidence"])
        habit_engine.record_device_event(
            "plug", "Đèn phòng", "power_off", source="user_telegram", now_ts=run_ts + 120.0
        )
        updated = database.get_learned_habit(hk)
        self.assertLess(float(updated["confidence"]), old_conf)
        self.assertEqual(int(updated["negative_count"]), 1)

        # Kiểm tra AI manage_learned_habit: tạo + tắt + bật + quên
        note_learn = habit_engine.execute_ai_habit_action({
            "action": "learn",
            "target": "Quạt thông gió",
            "device_action": "power_off",
            "duration_mins": 20,
        })
        self.assertIn("Đã ghi nhớ thói quen", note_learn)
        snap = habit_engine.get_habits_snapshot()
        exh = [x for x in snap["habits"] if x["target"] == "Quạt thông gió"]
        self.assertTrue(exh)
        hk_exh = exh[0]["habit_key"]

        habit_engine.execute_ai_habit_action({"action": "mute", "habit_key": hk_exh})
        self.assertEqual(database.get_learned_habit(hk_exh)["status"], "muted")
        habit_engine.execute_ai_habit_action({"action": "enable", "habit_key": hk_exh})
        self.assertEqual(database.get_learned_habit(hk_exh)["status"], "active")
        habit_engine.execute_ai_habit_action({"action": "forget", "habit_key": hk_exh})
        self.assertIsNone(database.get_learned_habit(hk_exh))


class TestThreePassPipelineAndDashboard(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._db_file = os.path.join(_TMP_DIR.name, f"{self.id().split('.')[-1]}.db")
        if os.path.exists(self._db_file):
            os.remove(self._db_file)
        config.DB_PATH = self._db_file
        database.init_db()

    async def test_3pass_mcp_fetch_and_habit_action(self):
        uid = 55123
        database.get_or_create_user(uid)
        database.add_chat_message(uid, "user", "Nhiệt độ phòng giờ bao nhiêu và đang phát bài gì?")

        p1_json = json.dumps({
            "user_intent": "Hỏi nhiệt độ phòng và bài nhạc đang phát",
            "direct_answer_strategy": "Đọc thông tin MCP phòng và trả lời",
            "raw_ideas": ["Đọc nhiệt độ và loa hiện tại"],
            "fetch_tools": ["get_room_climate", "get_speaker_state"],
            "habit_action": {"action": "learn", "target": "Đèn phòng", "device_action": "power_on", "trigger_time": "18:30"},
            "new_mood": "vui vẻ",
        }, ensure_ascii=False)
        p2_json = json.dumps({
            "messages": ["Phòng hiện tại 26.5°C, độ ẩm 64%. Loa đang phát Lo-fi Work ở mức 30% nhé!"]
        }, ensure_ascii=False)
        p3_json = json.dumps({
            "passed": True,
            "severity": "none",
            "categories": [],
            "already_fulfilled": False,
            "contradictions": [],
            "fix_hint": "",
        }, ensure_ascii=False)

        captured_prompts = []

        async def _fake_call(model, system_instruction="", contents=None, response_json=False, **kwargs):
            captured_prompts.append((system_instruction, contents))
            idx = len(captured_prompts)
            if idx == 1:
                return p1_json
            if idx == 2:
                return p2_json
            return p3_json

        with patch.object(gemini_client, "call_gemini_api", side_effect=_fake_call), \
             patch("vector_memory.retrieve_memory_context_with_status", new=AsyncMock(return_value={"memories": [], "memory_miss": False, "queries": []})), \
             patch("diary_helper.get_or_create_today_diary", new=AsyncMock(return_value="Trực phòng ổn định")), \
             patch("tts_client.is_tts_available", new=AsyncMock(return_value=False)), \
             patch("mcp_tools.execute_fetch_tools_for_ai", new=AsyncMock(return_value="[MCP] DHT11: 26.5°C / 64% | Loa: playing Lo-fi Work (30%)")) as mock_mcp:
            res = await gemini_client.chat_with_butler(uid, "Nhiệt độ phòng giờ bao nhiêu và đang phát bài gì?")

        mock_mcp.assert_awaited_once_with(["get_room_climate", "get_speaker_state"])
        self.assertEqual(res["messages"], ["Phòng hiện tại 26.5°C, độ ẩm 64%. Loa đang phát Lo-fi Work ở mức 30% nhé!"])
        self.assertEqual(res["habit_action"]["target"], "Đèn phòng")
        self.assertGreaterEqual(len(captured_prompts), 2)
        p2_user_txt = captured_prompts[1][1][-1]["parts"][0]["text"]
        self.assertIn("26.5°C", p2_user_txt)

    async def test_dashboard_persona_and_habits_routes(self):
        app = web_dashboard.create_app(bot=None, target_user_id=777)
        async with TestClient(TestServer(app)) as client:
            health = await client.get("/api/health")
            self.assertEqual(health.status, 200)
            hjson = await health.json()
            self.assertTrue(hjson["ok"])
            self.assertEqual(hjson["data"]["service"], "smart_butler")

            pin = web_dashboard.get_effective_pin()
            login = await client.post("/api/login", json={"pin": pin})
            self.assertEqual(login.status, 200)

            ov = await client.get("/api/overview")
            self.assertEqual(ov.status, 200)
            ov_data = (await ov.json())["data"]
            self.assertIn("persona", ov_data)
            self.assertIn("habits", ov_data)

            patch_p = await client.patch("/api/persona", json={
                "butler_name": "Friday",
                "owner_name": "Khoa",
                "self_pronoun": "tớ",
                "style": "companion",
                "weather_city": "Da_Nang",
                "ai_bio": "Bạn đồng hành yêu công nghệ.",
                "ai_personality": "Hài hước và thân thiện.",
                "speaking_style": "Câu ngắn tự nhiên.",
                "owner_bio": "Làm việc từ xa ban đêm.",
            })
            self.assertEqual(patch_p.status, 200)
            p_res = (await patch_p.json())["data"]
            self.assertEqual(p_res["butler_name"], "Friday")
            self.assertEqual(p_res["owner_name"], "Khoa")
            self.assertEqual(p_res["self_ref"], "tớ")
            self.assertEqual(p_res["style"], "companion")
            self.assertEqual(p_res["ai_bio"], "Bạn đồng hành yêu công nghệ.")
            self.assertEqual(p_res["ai_personality"], "Hài hước và thân thiện.")
            self.assertIn("Hài hước và thân thiện", p_res["prompt_preview"])

            j_res = await client.get("/api/journal")
            self.assertEqual(j_res.status, 200)
            j_data = (await j_res.json())["data"]
            self.assertNotIn("morning_dream", j_data)

    async def test_bot_cmd_status_smoke(self):
        uid = 888
        config.ALLOWED_USER_ID = uid
        database.get_or_create_user(uid)
        sent_texts = []
        fake_msg = MagicMock()
        fake_msg.from_user.id = uid

        async def _fake_answer(msg, text):
            sent_texts.append(text)

        with patch.object(butler_bot, "answer_markdown_safe", side_effect=_fake_answer):
            await butler_bot.cmd_status(fake_msg)
            await butler_bot.cmd_habits(fake_msg)

        self.assertEqual(len(sent_texts), 2)
        self.assertIn("NHÀ THÔNG MINH", sent_texts[0])
        self.assertIn("THÓI QUEN THIẾT BỊ TỰ HỌC", sent_texts[1])


class TestSyncedSmartHomeAndAntiRepetitionFeatures(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._db_file = os.path.join(_TMP_DIR.name, f"{self.id().split('.')[-1]}.db")
        if os.path.exists(self._db_file):
            os.remove(self._db_file)
        config.DB_PATH = self._db_file
        database.init_db()

    async def test_plug_and_fan_multistep_timers_and_dashboard_routes(self):
        smart_plugs.save_plugs_config([
            {"mac": "aa:bb:cc:dd:ee:11", "ip": "192.168.1.51", "name": "Quạt thông gió", "aliases": ["thông gió"]},
        ])
        plug_ctrl = smart_plugs.SmartPlugController()
        fan_ctrl = fan_controller.FanController()

        seq_p = plug_ctrl.schedule_sequence(
            "aa:bb:cc:dd:ee:11", start_on=True, due_at=2000000000.0, duration_minutes=10
        )
        self.assertIsNotNone(seq_p)
        self.assertEqual(len(plug_ctrl._timers["aa:bb:cc:dd:ee:11"]), 2)
        self.assertAlmostEqual(seq_p["off_due_at"], 2000000000.0 + 600.0)

        seq_f = fan_ctrl.schedule_sequence(
            "on", due_at=2000000000.0, speed_pct=20, duration_minutes=15, now_ts=1999999900.0
        )
        self.assertEqual(len(seq_f), 2)
        self.assertEqual(seq_f[0]["action"], "on")
        self.assertEqual(seq_f[1]["action"], "off")

        app = web_dashboard.create_app(bot=None, target_user_id=778)
        with patch.object(fan_controller, "get_controller", return_value=fan_ctrl), \
             patch.object(smart_plugs, "get_controller", return_value=plug_ctrl):
            async with TestClient(TestServer(app)) as client:
                pin = web_dashboard.get_effective_pin()
                await client.post("/api/login", json={"pin": pin})

                r_fan = await client.post(
                    "/api/fan/control",
                    json={"action": "timer", "value": True, "delay_minutes": 5, "duration_minutes": 10},
                )
                self.assertEqual(r_fan.status, 200)
                f_snap = (await r_fan.json())["data"]["fan"]
                self.assertTrue(f_snap.get("timers") or f_snap.get("timer"))

                r_plug = await client.post(
                    "/api/plugs/control",
                    json={"mac": "aa:bb:cc:dd:ee:11", "action": "timer", "value": True, "delay_minutes": 5, "duration_minutes": 10},
                )
                self.assertEqual(r_plug.status, 200)
                p_list = (await r_plug.json())["data"]["plugs"]
                self.assertEqual(len(p_list[0].get("timers") or []), 2)

    def test_anti_repetition_open_loops_and_call_decay(self):
        self.assertTrue(database.is_trivial_open_loop_topic("Bật quạt thông gió 10 phút"))
        self.assertTrue(database.is_trivial_open_loop_topic("Đi ăn sáng món ấm nóng"))
        self.assertFalse(database.is_trivial_open_loop_topic("Họp dự án quan trọng lúc 9h"))

        uid = 66101
        t0 = 1794990000.0
        database.apply_session_state_update(uid, {"call_status": "in_call"}, now_ts=t0)
        # 2 giờ sau trạng thái call vẫn giữ nếu chưa hết timeout, nhưng sau 4 giờ không cập nhật call phải tự ended
        database.apply_session_state_update(uid, {"user_activity": "đọc tài liệu"}, now_ts=t0 + 4 * 3600.0)
        st = database.get_session_state(uid, now_ts=t0 + 4 * 3600.0)
        self.assertEqual(st["call_status"], "ended")

        recent_bot = ["Tối nay bạn nhớ tắt điện thoại đi ngủ sớm nha!"]
        msgs, _, trimmed = gemini_client._trim_repetitive_trailing_sentences(
            ["Tôi đã bật đèn phòng lên rồi nhé. Tối nay nhớ tắt điện thoại đi ngủ sớm nha!"],
            recent_bot,
            user_text="bật đèn giúp mình",
        )
        self.assertTrue(trimmed)
        self.assertEqual(msgs, ["Tôi đã bật đèn phòng lên rồi nhé."])

    async def test_router_controller_settings_and_dashboard_status(self):
        router_controller.save_router_settings({
            "enabled": True,
            "router_url": "http://192.168.1.1/",
            "username": "root",
            "password": "secret-router-pass",
        })
        saved = router_controller.get_router_settings(mask_secret=True)
        self.assertEqual(saved["router_url"], "http://192.168.1.1")
        self.assertEqual(saved["username"], "root")
        self.assertTrue(saved["has_password"])
        self.assertEqual(saved["password"], "")

        rt_ctrl = router_controller.OpenWrtRouterController()
        blocked = await rt_ctrl.execute_ai_router_action({"action": "reboot"})
        self.assertIn("Từ chối", blocked)

        app = web_dashboard.create_app(bot=None, target_user_id=779)
        with patch.object(router_controller, "get_controller", return_value=rt_ctrl):
            async with TestClient(TestServer(app)) as client:
                pin = web_dashboard.get_effective_pin()
                await client.post("/api/login", json={"pin": pin})
                r_st = await client.get("/api/router/status?refresh=0")
                self.assertEqual(r_st.status, 200)
                body = await r_st.json()
                self.assertTrue(body["ok"])
                self.assertIn("router", body["data"])

    async def test_live_speaker_ws_and_manager(self):
        mgr = esp32_audio.ESP32AudioManager()
        gen = await mgr.start_live_stream(source="web_cast", label="Test Tab", client_ip="127.0.0.1")
        self.assertTrue(mgr.live_stream_active)
        self.assertEqual(mgr.status, "playing")
        pcm_chunk = b"\x10\x00" * 480
        self.assertTrue(mgr.push_live_pcm(pcm_chunk, live_gen=gen))
        self.assertEqual(len(mgr._live_pcm_buffer), len(pcm_chunk))
        await mgr.stop_live_stream(live_gen=gen, reason="unit_test")
        self.assertFalse(mgr.live_stream_active)

        app = web_dashboard.create_app(bot=None, target_user_id=780)
        with patch.object(esp32_audio, "get_manager", return_value=mgr):
            async with TestClient(TestServer(app)) as client:
                async with client.ws_connect("/api/jukebox/live_ws?source=web_cast&label=UnitWS") as ws:
                    ready = await ws.receive_json()
                    self.assertEqual(ready["type"], "live_ready")
                    self.assertEqual(ready["sample_rate"], esp32_audio.SAMPLE_RATE)
                    self.assertTrue(mgr.live_stream_active)
                    await ws.send_bytes(b"\x20\x00" * 240)
                    await ws.send_json({"type": "ping"})
                    pong = await ws.receive_json()
                    self.assertEqual(pong["type"], "pong")
                    self.assertTrue(pong["live"])
                    await ws.send_json({"type": "stop"})
                self.assertFalse(mgr.live_stream_active)

    def test_lan_presence_proc_fallbacks(self):
        proc_arp = (
            "IP address       HW type     Flags       HW address            Mask     Device\n"
            "192.168.1.1      0x1         0x2         50:64:2b:18:4f:cc     *        wlan0\n"
            "192.168.1.99     0x1         0x0         00:00:00:00:00:00     *        wlan0\n"
            "192.168.1.20     0x1         0x2         aa:bb:cc:dd:ee:20     *        usb0\n"
        )
        rows_wlan = lan_presence.parse_proc_net_arp(proc_arp, filter_iface="wlan0")
        self.assertEqual(len(rows_wlan), 1)
        self.assertEqual(rows_wlan[0]["ip"], "192.168.1.1")
        self.assertEqual(rows_wlan[0]["mac"], "50:64:2b:18:4f:cc")

        proc_route = (
            "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
            "wlan0\t00000000\t0101A8C0\t0003\t0\t0\t600\t00000000\t0\t0\t0\n"
            "wlan0\t0001A8C0\t00000000\t0001\t0\t0\t600\t00FFFFFF\t0\t0\t0\n"
        )
        iface, gw = lan_presence._read_proc_default_route("wlan0", route_text=proc_route)
        self.assertEqual(iface, "wlan0")
        self.assertEqual(gw, "192.168.1.1")


if __name__ == "__main__":
    unittest.main()
