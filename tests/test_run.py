import subprocess

import pytest

from src.run import REPO_ROOT, build_stages, main


def _flag(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_report_only_stages_share_the_same_results_and_prices_paths():
    stages = build_stages("report-only")
    assert len(stages) == 2
    report_argv = stages[0][1]
    detail_argv = stages[1][1]
    assert "src.report" in report_argv
    assert "src.detail" in detail_argv
    assert _flag(report_argv, "--results") == _flag(detail_argv, "--results")
    assert _flag(report_argv, "--prices") == _flag(detail_argv, "--prices")


def test_smoke_stages_chain_ingest_output_into_backtest_panel():
    stages = build_stages("smoke")
    assert [d for d, _ in stages] == [
        "Ingest the 3-dataset validation sample (real SEC download)",
        "Quick backtest on the sample panel (real yfinance calls)",
        "Generate a smoke-test HTML report (not the real deliverable)",
    ]
    ingest_argv, backtest_argv, report_argv = (argv for _, argv in stages)
    assert "src.ingest" in ingest_argv
    assert "--full" not in ingest_argv  # sample mode, not full
    assert "--quick" in backtest_argv
    # backtest's --panel must be ingest's (hardcoded) sample-mode output path.
    assert _flag(backtest_argv, "--panel").endswith("13f_panel_sample.parquet")
    # backtest's --out/--prices-cache must be exactly what report reads next.
    assert _flag(backtest_argv, "--out") == _flag(report_argv, "--results")
    assert _flag(backtest_argv, "--prices-cache") == _flag(report_argv, "--prices")
    # Smoke mode never writes to the real committed deliverable paths.
    assert "results/smoke" in _flag(report_argv, "--out").replace("\\", "/")


def test_full_stages_chain_ingest_output_into_backtest_panel_and_on_to_report_and_detail():
    stages = build_stages("full")
    assert len(stages) == 4
    ingest_argv, backtest_argv, report_argv, detail_argv = (argv for _, argv in stages)
    assert "--full" in ingest_argv
    assert _flag(backtest_argv, "--panel").endswith("13f_panel_full.parquet")
    assert _flag(backtest_argv, "--out") == _flag(report_argv, "--results") == _flag(detail_argv, "--results")
    assert _flag(backtest_argv, "--prices-cache") == _flag(report_argv, "--prices") == _flag(detail_argv, "--prices")


def test_build_stages_rejects_unknown_mode():
    with pytest.raises(ValueError):
        build_stages("bogus")


def test_main_runs_every_stage_with_cwd_anchored_to_repo_root(monkeypatch, capsys):
    calls = []

    def fake_run(argv, check, cwd):
        calls.append((argv, check, cwd))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("sys.argv", ["run.py", "--mode", "report-only"])

    main()

    assert len(calls) == 2
    for argv, check, cwd in calls:
        assert check is True
        assert cwd == REPO_ROOT
    out = capsys.readouterr().out
    assert "[1/2]" in out
    assert "[2/2]" in out
    assert "Pipeline complete (report-only)" in out


def test_main_exits_cleanly_on_a_failing_stage_without_a_raw_traceback(monkeypatch, capsys):
    def fake_run(argv, check, cwd):
        if "src.detail" in argv:
            raise subprocess.CalledProcessError(1, argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("sys.argv", ["run.py", "--mode", "report-only"])

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "FAILED at stage" in out
    assert "Pipeline complete" not in out
