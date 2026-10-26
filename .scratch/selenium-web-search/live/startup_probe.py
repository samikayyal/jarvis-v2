from pathlib import Path
import tempfile
from jarvis_personal_runtime.web_worker import SeleniumPageReader

with tempfile.TemporaryDirectory(prefix="jarvis-startup-probe-") as folder:
    reader = SeleniumPageReader(Path(folder), 20, 8000)
    try:
        reader._start()
        reader.driver.get("chrome://sandbox")
        print(reader.driver.find_element("tag name", "body").text)
    finally:
        reader.close()
