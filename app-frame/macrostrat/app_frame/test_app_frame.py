from typer.testing import CliRunner

from macrostrat.app_frame import Application

runner = CliRunner()


def test_app_frame_setup():
    main = _setup_app()
    assert main.app.name == "Test App"


def test_app_frame_cli_invocation():
    main = _setup_app()
    result = runner.invoke(main, ["print-name"])
    assert "Test App" in result.output
    assert result.exit_code == 0


def _setup_app():
    app = Application(
        "Test App",
        command_name="test-app",
        log_modules=["test_app"],
    )

    cmd = app.control_command()

    @cmd.command(name="print-name", help="Print the name of the app")
    def main():
        print("Test App")

    return cmd


def test_app_frame_help():
    main = _setup_app()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0, result.output
    assert "print-name" in result.output
