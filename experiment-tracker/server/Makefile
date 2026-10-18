.PHONY: build-ApiFunction

build-ApiFunction:
	python3 -m pip install --requirement requirements.txt --target "$(ARTIFACTS_DIR)" --platform manylinux2014_x86_64 --implementation cp --python-version 3.12 --only-binary=:all: --upgrade
	python3 -c 'import pathlib, shutil; target = pathlib.Path("$(ARTIFACTS_DIR)"); shutil.copytree("tracker", target / "tracker", dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")); shutil.copy2("lambda_handler.py", target / "lambda_handler.py")'
