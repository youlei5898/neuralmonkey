#!/bin/bash

set -Ex
trap 'echo -e "\033[1;31mMypy spotted errors!\033[0m"' ERR

mypy --ignore-missing-imports --follow-imports=skip -p neuralmonkey
