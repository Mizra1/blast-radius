FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.lock /build/requirements.lock
RUN grep -v -i -E '^(mypy|mypy_extensions|ruff|pre_commit|pdoc|pandas-stubs|types-|cfgv|distlib|identify|nodeenv|virtualenv|filelock|platformdirs|python-discovery|pyperf|librt|ast_serialize|setuptools|vcs-versioning|Jinja2|MarkupSafe|markdown2|pathspec)' /build/requirements.lock > /build/req.txt \
 && pip install --no-cache-dir -r /build/req.txt coverage
COPY build/ /build/
RUN python /build/fetch.py && (python /build/build_map.py || echo "map build failed; selector will run everything" >&2) \
 && chmod -R a+rX /opt /app 2>/dev/null || true
COPY selector.py runner.py /app/
COPY overlay/ /app/overlay/
RUN chmod -R a+rX /app
CMD ["python", "/app/selector.py"]
