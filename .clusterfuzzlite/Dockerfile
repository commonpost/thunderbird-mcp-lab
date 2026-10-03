# ClusterFuzzLite build image: Python fuzzing with atheris. Pinned by digest (Dependabot proposes updates).
FROM gcr.io/oss-fuzz-base/base-builder-python@sha256:cfcc123c40d4ecab777ac0f32f868177cb752950bf9f5d880f55e452adda5106
COPY . $SRC/thunderbird-mcp-lab
WORKDIR $SRC/thunderbird-mcp-lab
COPY .clusterfuzzlite/build.sh $SRC/
