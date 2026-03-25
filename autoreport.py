import xml.etree.ElementTree as ET
from datetime import datetime

def convert_log_to_junit(log_file, output_file):
    root = ET.Element("testsuite", name="TestSuite", timestamp=datetime.now().isoformat())
    
    with open(log_file, 'r') as f:
        for line in f:
            if "PASS" in line:
                test_case = ET.SubElement(root, "testcase", name=line.strip())
            elif "FAIL" in line:
                test_case = ET.SubElement(root, "testcase", name=line.strip())
                ET.SubElement(test_case, "failure", message="Test failed")
            elif "ERROR" in line:
                test_case = ET.SubElement(root, "testcase", name=line.strip())
                ET.SubElement(test_case, "error", message="Test error")
    
    tree = ET.ElementTree(root)
    tree.write(output_file, encoding='utf-8', xml_declaration=True)

convert_log_to_junit("autotest.log", "junit.xml")
