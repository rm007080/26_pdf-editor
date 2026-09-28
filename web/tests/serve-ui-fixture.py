"""実データと設定を分離した、ブラウザ確認用のPDF編集サーバー。"""

from io import BytesIO
from pathlib import Path
import sys
import tempfile

import pikepdf
import uvicorn

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.main import create_app
from app.session import Session


def sample_pdf() -> bytes:
    with pikepdf.Pdf.new() as pdf:
        for index in range(10):
            width, height = (960, 540) if index % 2 == 0 else (595, 842)
            page = pdf.add_blank_page(page_size=(width, height))
            page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(
                F1=pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1,
                                     BaseFont=pikepdf.Name.Helvetica)))
            page.Contents = pikepdf.Stream(pdf, (
                f"q 0.90 0.94 0.97 rg 0 0 {width} {height} re f Q "
                f"BT /F1 32 Tf 28 44 Td (PDF layout test - page {index + 1}) Tj ET"
            ).encode("ascii"))
        output = BytesIO()
        pdf.save(output)
        return output.getvalue()


class FixtureSession(Session):
    def startup(self) -> None:
        super().startup()
        status, intake = self.accept_upload(sample_pdf(), "layout-sample.pdf")
        assert status == 200, intake
        status, result = self.apply_command({"type": "append_documents", "document_ids": [intake["document_id"]]})
        assert status == 200, result


if __name__ == "__main__":
    scratch = ROOT / "tmp"
    scratch.mkdir(exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="ui-smoke-", dir=scratch)).resolve()
    assert run_dir.is_relative_to(scratch.resolve())
    session = FixtureSession(temp_dir=run_dir / "session")
    app = create_app(session, settings_path=run_dir / "settings.json", log_dir=run_dir / "logs")
    uvicorn.run(app, host="127.0.0.1", port=0)
