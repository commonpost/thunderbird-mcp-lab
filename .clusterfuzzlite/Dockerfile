# ClusterFuzzLite build image: Python fuzzing with atheris. Pinned by digest (Dependabot proposes updates).
FROM gcr.io/oss-fuzz-base/base-builder-python@sha256:aa1326f311d899ed3ca050a89b6c1e3528e2aa86be1ce5180aaddf3567547a99
COPY . $SRC/thunderbird-mcp-lab
WORKDIR $SRC/thunderbird-mcp-lab
COPY .clusterfuzzlite/build.sh $SRC/
