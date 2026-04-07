import argparse
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path


def convert_session_json_to_junit(report_json: str, output_xml: str) -> None:
    report = json.loads(Path(report_json).read_text(encoding="utf-8"))
    stages = report.get("stages", [])

    tests = len(stages)
    failures = sum(1 for s in stages if s.get("status") != "passed")

    suite = ET.Element(
        "testsuite",
        name="voip-autotest",
        timestamp=datetime.now(timezone.utc).isoformat(),
        tests=str(tests),
        failures=str(failures),
        errors="0",
    )

    for stage in stages:
        case = ET.SubElement(
            suite,
            "testcase",
            classname="voip.session",
            name=stage.get("name", "unknown"),
        )
        status = stage.get("status", "failed")
        details = stage.get("details", "")
        if status != "passed":
            failure = ET.SubElement(case, "failure", message="stage failed")
            failure.text = details
        else:
            ET.SubElement(case, "system-out").text = details

    ET.ElementTree(suite).write(output_xml, encoding="utf-8", xml_declaration=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert session JSON report to GitLab-compatible JUnit XML")
    parser.add_argument("--input", required=True, help="Path to session-report.json")
    parser.add_argument("--output", required=True, help="Path to junit.xml")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    convert_session_json_to_junit(args.input, args.output)
