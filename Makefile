RUFF_COMMAND = uvx ruff
TY_COMMAND = export TY_UV=scripts uvx ty check

test:
	uv run pytest

.PHONY: cq
cq:
	${RUFF_COMMAND} check . && ${RUFF_COMMAND} format --check . && ${TY_COMMAND}

# NOTE: cqf == "Code quality fix"
.PHONY: cqf
cqf:
	${RUFF_COMMAND} format . && ${RUFF_COMMAND} check --fix . && ${TY_COMMAND}