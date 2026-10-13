# ClusterFuzzLite build image: Python fuzzing with atheris. Pinned by digest (Dependabot proposes updates).
FROM gcr.io/oss-fuzz-base/base-builder-python@sha256:88bcf57cb6507ccc95cb5fcc3afeece6278b24880ec483a17f044a154277f9d0
COPY . $SRC/thunderbird-mcp-lab
WORKDIR $SRC/thunderbird-mcp-lab
COPY .clusterfuzzlite/build.sh $SRC/
