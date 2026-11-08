import json

from invoice_mgmt.cli import main
from invoice_mgmt.evaluate import run, same
from invoice_mgmt.readers import read_document


def test_testset_generates_readable_files_and_truth(tmp_path, capsys):
    out = tmp_path / "ts"
    assert main(["testset", "-o", str(out)]) == 0
    truth = json.loads((out / "truth.json").read_text())
    assert len(truth) == 12
    for name, entry in truth.items():
        doc = read_document(out / name)
        if any(k in entry["kind"] for k in ("SCAN", "PHOTO")):
            assert not doc.has_text and doc.can_render
        else:
            assert doc.has_text
            assert entry["data"]["invoice_number"] in doc.text.replace("\n", " ")
    # rules-only baseline runs end to end and produces a score
    res = run(out, engine="rules", out=open(tmp_path / "log.txt", "w"))
    assert res["total"] > 100 and 0 < res["correct"] < res["total"]


def test_same():
    assert same(9565.0, "9,565.00") and same("Contoso Ltd.", "contoso ltd") and not same(1.0, None)
