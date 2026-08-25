from tools import format_symbol_result


def test_format_symbol_result_with_matches():
    """Test formatting a successful search with one or more symbol matches."""
    sample_res = {
        "matches": [
            {
                "id": 1,
                "name": "parse_header",
                "kind": "function",
                "file": "utils.py",
                "start_line": 3,
                "end_line": 6,
                "signature": "def parse_header(raw_header: str) -> dict",
                "docstring": "Parse HTTP headers into a dictionary.",
            }
        ],
        "did_you_mean": [],
    }

    formatted = format_symbol_result("parse_header", sample_res)

    assert "Found 1 match(es) for 'parse_header':" in formatted
    assert "function `parse_header` in `utils.py` (lines 3-6)" in formatted
    assert "Signature: def parse_header(raw_header: str) -> dict" in formatted
    assert "Docstring: Parse HTTP headers into a dictionary." in formatted


def test_format_symbol_result_with_did_you_mean():
    """Test formatting spelling suggestions when no exact match was found."""
    sample_res = {
        "matches": [],
        "did_you_mean": ["parse_header", "parse_cookie"],
    }

    formatted = format_symbol_result("parse_headr", sample_res)

    assert "No exact match for 'parse_headr'." in formatted
    assert "Did you mean: parse_header, parse_cookie?" in formatted


def test_format_symbol_result_empty():
    """Test formatting when no matches or suggestions exist."""
    sample_res = {
        "matches": [],
        "did_you_mean": [],
    }

    formatted = format_symbol_result("non_existent_symbol", sample_res)

    assert formatted == "No matches found for 'non_existent_symbol'."


def test_normalize_xml_tool_call():
    from sandbox_utils import _normalize_xml_tool_call

    # Test read_file XML
    xml_read = """
    I will read the test file now.
    <tool_call>
    <tool_name>read_file</tool_name>
    <tool>tests/test_trans.py</tool>
    <tool_call>1, 49</tool_call>
    </tool_call>
    """
    normalized = _normalize_xml_tool_call(xml_read)
    assert 'ACTION: read_file("tests/test_trans.py", 1, 49)' in normalized

    # Test run_bash_command XML
    xml_bash = """
    <tool_call>
    <tool_name>run_bash_command</tool_name>
    <tool>pytest tests/test_format.py</tool>
    </tool_call>
    """
    normalized_bash = _normalize_xml_tool_call(xml_bash)
    assert 'ACTION: run_bash_command("pytest tests/test_format.py")' in normalized_bash
