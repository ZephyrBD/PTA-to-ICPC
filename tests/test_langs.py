from pta2ccs.models import sanitize_id
from pta2ccs.pta.langs import (
    STD_EXTENSIONS,
    extensions_for,
    primary_extension,
    std_language_id,
)


def test_std_language_id_merges_mapped_enums():
    assert std_language_id("GCC") == "c"
    assert std_language_id("MODERN_GCC") == "c"
    assert std_language_id("CLANG") == "c"
    assert std_language_id("GXX") == "cpp"
    assert std_language_id("CLANGXX") == "cpp"
    assert std_language_id("JAVAC") == "java"
    assert std_language_id("PYTHON2") == "python2"
    assert std_language_id("PYTHON3") == "python3"
    assert std_language_id("RUBY") == "ruby"
    assert std_language_id("GO") == "go"
    assert std_language_id("GHC") == "haskell"
    assert std_language_id("FPC") == "pascal"


def test_std_language_id_accepts_display_names():
    # submission rows carry COMPILER_LANGUAGES display names, not enums
    assert std_language_id("C++ (g++)") == "cpp"
    assert std_language_id("C (gcc c11)") == "c"
    assert std_language_id("Java (javac)") == "java"
    assert std_language_id("Python (python3)") == "python3"
    assert std_language_id("Pascal (fpc)") == "pascal"


def test_std_language_id_unknown_passthrough():
    # unlisted enums / names keep their sanitized original id
    assert std_language_id("PYPY3") == "PYPY3"
    assert std_language_id("C# (dotnet)") == sanitize_id("C# (dotnet)")
    assert std_language_id("MYLANG") == "MYLANG"


def test_std_language_id_falsy_becomes_unknown():
    assert std_language_id(None) == "unknown"
    assert std_language_id("") == "unknown"


def test_extensions_for_known_ids():
    assert extensions_for("cpp") == ["cc", "cpp", "cxx", "c++", "C"]
    assert extensions_for("c") == ["c"]
    assert extensions_for("java") == ["java"]
    assert extensions_for("python3") == ["py"]
    assert extensions_for("python2") == ["py"]
    assert extensions_for("ruby") == ["rb"]
    assert extensions_for("ada") == ["adb", "ads"]


def test_extensions_for_unknown_is_empty():
    assert extensions_for("not-a-language") == []
    assert extensions_for("") == []


def test_std_extensions_table_matches_spec_appendix():
    assert set(STD_EXTENSIONS) == {
        "ada", "c", "cpp", "csharp", "go", "haskell", "java", "javascript",
        "kotlin", "objectivec", "pascal", "php", "prolog", "python2",
        "python3", "ruby", "rust", "scala",
    }


def test_primary_extension_first_item_with_dot():
    assert primary_extension("python3") == ".py"
    assert primary_extension("cpp") == ".cc"  # appendix order: cc first
    assert primary_extension("c") == ".c"


def test_primary_extension_unknown_falls_back_to_txt():
    assert primary_extension("not-a-language") == ".txt"
