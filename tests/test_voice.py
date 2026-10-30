"""Tests of the voice assistant: the answers (navplotter/assistant.py) and the service (navplotter/voice.py) with fake
programs in place of the recorder, whisper-server, Piper and the player."""
import datetime
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from navplotter import assistant as A  # noqa: E402
from navplotter import voice as V  # noqa: E402

NOW = datetime.datetime(2026, 10, 30, 15, 0)


def boat(**kw):
    state = {"own": {"fix": True, "lat": 45.4300, "lon": 12.3300, "sog": 5.0, "cog": 135.0, "cogValid": True},
             "targets": [
                 {"id": "A7C94F1B", "name": "Gejsha", "type": 0, "dist": 1.2, "brg": 80, "sog": 4.0, "stale": False,
                  "alarm": False},
                 {"id": "0000ABCD", "name": "Pippa", "type": 0, "dist": 0.1, "brg": 200, "sog": 0.0, "stale": False,
                  "alarm": True, "cpaValid": True, "cpa": 0.05, "tcpa": 300},
                 {"id": "11112222", "name": "Vecchio", "type": 0, "dist": 0.5, "brg": 10, "sog": 0, "stale": True}],
             "alarm": True, "alarmName": "Pippa"}
    msgs = {"messages": [{"mine": False, "unread": True, "name": "Pippa", "text": "ci vediamo al porto", "peer": "0000ABCD"},
                         {"mine": True, "unread": False, "name": "Pippa", "text": "ok"}]}
    ud = {"wps": [{"id": 1, "name": "Boa", "lat": 45.4300, "lon": 12.4000},
                  {"id": 2, "name": "Porto", "lat": 45.5000, "lon": 12.4000}],
          "dest": 1, "routes": [{"id": 1, "name": "Gita", "wps": [1, 2]}], "route": 1}
    d = {"state": state, "messages": msgs, "userdata": ud}
    d.update(kw)
    return d


class Answers(unittest.TestCase):
    def ask(self, q, data=None):
        return A.answer(q, data or boat(), NOW)

    def test_position_and_speed(self):
        self.assertEqual(self.ask("Dove siamo?")[0], "Siamo a 45 gradi 25,8 primi nord, 12 gradi 19,8 primi est.")
        self.assertEqual(self.ask("A che velocità andiamo?")[0], "Andiamo a 5 nodi, rotta 135 gradi, verso sud-est.")

    def test_waypoint_with_eta_and_route(self):
        r = self.ask("Quanto manca al prossimo waypoint?")[0]
        self.assertTrue(r.startswith("Boa è a 2,9 miglia, 90 gradi, verso est. A 5 nodi ci arriviamo in 35 minuti, verso le 15:35."), r)
        self.assertIn("Fino alla fine della rotta Gita mancano 7,2 miglia, con 2 waypoint.", r)

    def test_no_destination_no_fix(self):
        d = boat()
        d["userdata"]["dest"] = None
        self.assertIn("Non c'è un waypoint", self.ask("quanto manca all'arrivo", d)[0])
        d = boat()
        d["state"]["own"]["fix"] = False
        self.assertEqual(self.ask("dove siamo", d)[0], "Il GPS non ha ancora la posizione.")

    def test_nearby_skips_stale_and_warns(self):
        r = self.ask("Chi c'è vicino?")[0]
        self.assertTrue(r.startswith("Ci sono 2 nodi vicini. Il più vicino è Pippa, a 190 metri, direzione 200 gradi, verso sud, fermo."), r)
        self.assertIn("allarme collisione con Pippa", r)

    def test_alarm(self):
        self.assertEqual(self.ask("c'è qualche allarme?")[0],
                         "Allarme collisione con Pippa, a 190 metri, direzione 200 gradi, verso sud. "
                         "Passaggio più vicino a 90 metri tra 5 minuti.")
        d = boat()
        d["state"]["alarm"] = False
        for t in d["state"]["targets"]:
            t["alarm"] = False
        self.assertEqual(self.ask("allarmi?", d)[0], "Nessun allarme.")

    def test_messages(self):
        self.assertEqual(self.ask("Ci sono messaggi?")[0], "Hai un messaggio nuovo. Da Pippa: ci vediamo al porto.")

    def test_send_message(self):
        r, act = self.ask("Manda un messaggio a tutti: arriviamo alle sei")
        self.assertEqual((r, act), ("Mando a tutti: arriviamo alle sei.", ("send", "00000000", "", "arriviamo alle sei")))
        r, act = self.ask("Invia un messaggio a Pippa che dice siamo in ritardo")
        self.assertEqual(act, ("send", "0000ABCD", "Pippa", "siamo in ritardo"))
        r, act = self.ask("manda un messaggio a Pluto ciao")
        self.assertIsNone(act)
        self.assertIn("Non trovo pluto", r)

    def test_time_help_and_unknown(self):
        self.assertEqual(self.ask("Che ore sono?")[0], "Sono le 15:00.")
        self.assertEqual(self.ask("cosa posso chiederti")[0], A.HELP)
        self.assertEqual(self.ask("raccontami una barzelletta"), (None, None))

    def test_llm_context(self):
        msgs = A.llm_messages("che facciamo?", boat(), NOW)
        ctx = msgs[0]["content"]
        self.assertIn("Prossimo waypoint: Boa", ctx)
        self.assertIn("Vicino: Pippa (barca) a 190 metri", ctx)
        self.assertIn("IN ALLARME COLLISIONE", ctx)
        self.assertNotIn("Vecchio", ctx)
        self.assertEqual(msgs[1], {"role": "user", "content": "che facciamo?"})


def serve(handler):
    s = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


class Service(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.sent = []
        t = self

        class Node(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _j(self, o):
                b = json.dumps(o).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                data = boat()
                self._j({"/api/state": data["state"], "/api/messages": data["messages"],
                         "/api/userdata": data["userdata"]}[self.path])

            def do_POST(self):
                t.sent.append(self.rfile.read(int(self.headers["Content-Length"])).decode())
                self._j({"ok": True})

        self.heard = " Quanto manca al prossimo waypoint?"

        class Whisper(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                t.audio = body
                b = json.dumps({"text": t.heard}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

        self.node, self.whisper = serve(Node), serve(Whisper)
        # Fake programs: the recorder writes a file and waits for ^C, Piper writes what it was given, the player logs
        self.played = os.path.join(d, "played.log")
        prog = lambda name, code: (open(os.path.join(d, name), "w").write("#!%s\n%s" % (sys.executable, code)),
                                   os.chmod(os.path.join(d, name), 0o755), os.path.join(d, name))[2]
        rec = prog("rec", "import sys,signal,time\nopen(sys.argv[-1],'wb').write(b'RIFFfakewav'+bytes(2000))\n"
                          "signal.signal(signal.SIGINT, lambda *a: sys.exit(0))\ntime.sleep(30)\n")
        piper = prog("piper", "import sys\nout=sys.argv[sys.argv.index('--output_file')+1]\n"
                              "open(out,'w').write(sys.stdin.read())\n")
        play = prog("play", "import sys\nopen(%r,'a').write(open(sys.argv[1],'rb').read()[:200].decode('utf-8','replace')+'\\n')\n" % self.played)
        parts = types.SimpleNamespace(whisper_server=None, whisper_model="ggml-small.bin", piper=piper,
                                      voice="it.onnx", llama_server=None, llm_model=None, record=rec, play=play)
        args = types.SimpleNamespace(home=d, node="http://127.0.0.1:%d" % self.node.server_address[1],
                                     stt_port=self.whisper.server_address[1], llm_port=1)
        self.v = V.Voice(args, parts)
        self.v.stt = types.SimpleNamespace(ok=True)

    def tearDown(self):
        self.node.shutdown()
        self.whisper.shutdown()
        self.tmp.cleanup()

    def wait_idle(self):
        for _ in range(100):
            if self.v.state == "idle":
                return
            time.sleep(0.05)
        self.fail("still %s" % self.v.state)

    def test_listen_answer_speak(self):
        self.v.start_listening()
        self.assertEqual(self.v.state, "listening")
        time.sleep(0.3)
        self.v.stop_listening()
        self.wait_idle()
        st = self.v.status()
        self.assertEqual(st["heard"], "Quanto manca al prossimo waypoint?")
        self.assertTrue(st["reply"].startswith("Boa è a 2,9 miglia"), st["reply"])
        self.assertIn(b"RIFFfakewav", self.audio)          # the recording went to whisper-server
        played = open(self.played).read()
        self.assertTrue(played.startswith("RIFF"))         # the beep
        self.assertIn("Boa è a 2,9 miglia", played)        # then the answer, through Piper

    def test_ask_sends_message(self):
        self.v.ask("manda un messaggio a tutti arriviamo alle sei")
        self.wait_idle()
        self.assertEqual(self.sent, ["to=00000000&name=&text=arriviamo+alle+sei"])
        self.assertEqual(self.v.status()["reply"], "Mando a tutti: arriviamo alle sei.")

    def test_unknown_without_llm_and_silence(self):
        self.v.ask("raccontami una barzelletta")
        self.wait_idle()
        self.assertTrue(self.v.status()["reply"].startswith("Non ho capito. Puoi chiedermi"))
        self.heard = " [BLANK_AUDIO]"
        self.v.start_listening()
        time.sleep(0.3)
        self.v.stop_listening()
        self.wait_idle()
        self.assertEqual(self.v.status()["reply"], "Non ho sentito niente.")


if __name__ == "__main__":
    unittest.main()
