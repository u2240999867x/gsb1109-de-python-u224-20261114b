"""
Tests for utils/plan_archive.py, which feeds real recordings to the planner.
"""
import importlib.util
import json
from pathlib import Path
import sys

from hpack import Encoder


def load_cli():
    path = Path(__file__).resolve().parents[1] / "utils" / "plan_archive.py"
    spec = importlib.util.spec_from_file_location("plan_archive", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["plan_archive"] = module
    spec.loader.exec_module(module)
    return module


def write_recording(tmp_path, document):
    target = tmp_path / "recording.json"
    target.write_text(json.dumps(document), encoding="utf-8")
    return target


def make_blocks(headers, table_size):
    encoder = Encoder()
    encoder.header_table_size = table_size
    return [encoder.encode(case, huffman=False).hex() for case in headers]


class TestPlanArchiveCLI:
    def test_plan_and_replay_a_recording(self, tmp_path, capsys):
        cli = load_cli()
        headers = [[("x-a", "1")], [("x-b", "2")], [("x-c", "3")]]
        recording = write_recording(
            tmp_path,
            {
                "initial_table_size": 256,
                "blocks": make_blocks(headers, 256),
                "targets": [{"block": 2}],
            },
        )

        exit_code = cli.main([str(recording)])
        output = json.loads(capsys.readouterr().out)

        assert exit_code == 0
        assert output["replay"]["ok"] is True
        assert output["plan"]["blocks"] == [0, 1, 2]
        assert output["plan"]["total_size"] > 0

    def test_explicit_target_fields_are_checked(self, tmp_path, capsys):
        cli = load_cli()
        recording = write_recording(
            tmp_path,
            {
                "initial_table_size": 256,
                "blocks": make_blocks([[("x-a", "1")]], 256),
                "targets": [{"block": 0, "fields": [["x-a", "wrong"]]}],
            },
        )

        assert cli.main([str(recording)] ) == 1
        output = json.loads(capsys.readouterr().out)
        assert output["replay"]["targets"]["0"]["match"] is False

    def test_checkpoint_size_override_is_used(self, tmp_path, capsys):
        cli = load_cli()
        recording = write_recording(
            tmp_path,
            {
                "initial_table_size": 64,
                "blocks": make_blocks([[("x-a", "1")], [("x-b", "2")], [("x-c", "3")]], 64),
                "checkpoints": [{"position": 2, "size": 1}],
                "targets": [{"block": 2}],
            },
        )

        assert cli.main([str(recording)]) == 0
        output = json.loads(capsys.readouterr().out)
        assert output["plan"]["checkpoints"] == [2]
        assert output["plan"]["blocks"] == [2]
