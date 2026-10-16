from ptapatch.pta.verdicts import PTA_STATUS_NAME_MAP, PTA_STATUS_VERDICT_MAP, PTA_VERDICT_MAP


def test_known_verdicts_map_correctly():
    assert PTA_VERDICT_MAP["答案正确"] == ("AC", True, False)
    assert PTA_VERDICT_MAP["答案错误"] == ("WA", False, True)
    assert PTA_VERDICT_MAP["格式错误"] == ("PE", False, True)
    assert PTA_VERDICT_MAP["运行超时"] == ("TLE", False, True)
    assert PTA_VERDICT_MAP["内存超限"] == ("MLE", False, True)
    assert PTA_VERDICT_MAP["输出超限"] == ("OLE", False, True)
    assert PTA_VERDICT_MAP["编译错误"] == ("CE", False, False)
    assert PTA_VERDICT_MAP["系统错误"] == ("SE", False, False)


def test_judging_string_maps_to_none_verdict():
    assert PTA_VERDICT_MAP["正在评测"] == (None, False, False)


def test_extra_verdicts_observed_in_frontend_enum():
    assert PTA_VERDICT_MAP["段错误"] == ("RE", False, True)
    assert PTA_VERDICT_MAP["运行时错误"] == ("RE", False, True)
    assert PTA_VERDICT_MAP["浮点错误"] == ("RE", False, True)
    assert PTA_VERDICT_MAP["非零返回"] == ("RE", False, True)
    assert PTA_VERDICT_MAP["多种错误"] == ("RE", False, True)
    assert PTA_VERDICT_MAP["样例错误"] == ("WA", False, True)
    assert PTA_VERDICT_MAP["未作答"] == ("WA", False, True)
    assert PTA_VERDICT_MAP["部分正确"] == ("PA", False, True)
    assert PTA_VERDICT_MAP["运行总时长超时"] == ("TLE", False, True)
    assert PTA_VERDICT_MAP["内部错误"] == ("SE", False, False)
    assert PTA_VERDICT_MAP["判题程序错误"] == ("SE", False, False)
    assert PTA_VERDICT_MAP["交互程序错误"] == ("SE", False, False)
    assert PTA_VERDICT_MAP["等待评测"] == (None, False, False)
    assert PTA_VERDICT_MAP["正在重测"] == (None, False, False)
    assert PTA_VERDICT_MAP["已被覆盖"] == (None, False, False)
    assert PTA_VERDICT_MAP["跳过"] == (None, False, False)
    assert PTA_VERDICT_MAP["运行结束"] == (None, False, False)


def test_status_name_map_matches_frontend_submission_status_enum():
    expected = {
        "ACCEPTED",
        "CHECKER_ERROR",
        "COMPILE_ERROR",
        "FLOAT_POINT_EXCEPTION",
        "INTERNAL_ERROR",
        "INTERACTOR_ERROR",
        "JUDGING",
        "MEMORY_LIMIT_EXCEEDED",
        "MULTIPLE_ERROR",
        "NEUTRAL",
        "NON_ZERO_EXIT_CODE",
        "NO_ANSWER",
        "OUTPUT_LIMIT_EXCEEDED",
        "OVERRIDDEN",
        "PARTIAL_ACCEPTED",
        "PRESENTATION_ERROR",
        "REJUDGING",
        "RUNTIME_ERROR",
        "SAMPLE_ERROR",
        "SEGMENTATION_FAULT",
        "SKIPPED",
        "TIME_LIMIT_EXCEEDED",
        "TOTAL_TIME_LIMIT_EXCEEDED",
        "WAITING",
        "WRONG_ANSWER",
    }
    assert set(PTA_STATUS_NAME_MAP) == expected


def test_every_status_label_resolves_in_verdict_map():
    for status, label in PTA_STATUS_NAME_MAP.items():
        assert label in PTA_VERDICT_MAP, f"missing verdict for {status} label {label!r}"


def test_status_verdict_map_is_composition_of_name_and_verdict_maps():
    for status, label in PTA_STATUS_NAME_MAP.items():
        assert PTA_STATUS_VERDICT_MAP[status] == PTA_VERDICT_MAP[label]


def test_live_observed_statuses_are_mapped():
    for status in ["ACCEPTED", "WRONG_ANSWER", "COMPILE_ERROR", "SEGMENTATION_FAULT"]:
        assert status in PTA_STATUS_VERDICT_MAP
    assert PTA_STATUS_VERDICT_MAP["ACCEPTED"] == ("AC", True, False)
    assert PTA_STATUS_VERDICT_MAP["SEGMENTATION_FAULT"] == ("RE", False, True)


def test_only_pending_or_ignored_verdicts_have_none_type():
    nones = {label for label, value in PTA_VERDICT_MAP.items() if value[0] is None}
    assert nones == {"正在评测", "等待评测", "正在重测", "已被覆盖", "跳过", "运行结束"}


def test_solved_exactly_when_accepted_and_penalties_only_for_failed_judged():
    for label, (judgement_type, solved, penalty) in PTA_VERDICT_MAP.items():
        assert solved == (judgement_type == "AC"), label
        if judgement_type in (None, "CE", "SE"):
            assert not penalty, label
        elif judgement_type != "AC":
            assert penalty, label
