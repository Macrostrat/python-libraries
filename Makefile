.PHONY: install status changeset version build publish test format

all: install

install:
	uv run mono install

status:
	uv run mono status

changeset:
	uv run mono changeset

version:
	uv run mono version

build:
	uv run mono build

publish:
	uv run mono publish

format:
	uv run isort .
	uv run black .

test:
	uv run pytest -s -x
