# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Guillaume Laforest
# ClusterFuzzLite build image: Python fuzzing with atheris. Pinned by digest (Dependabot proposes updates).
FROM gcr.io/oss-fuzz-base/base-builder-python@sha256:ed78b82bdc5a9fde750bc83f50594042a8918f2ddb5a2f6e7b1a4b8ad79c893c
COPY . $SRC/thunderbird-mcp-lab
WORKDIR $SRC/thunderbird-mcp-lab
COPY .clusterfuzzlite/build.sh $SRC/
