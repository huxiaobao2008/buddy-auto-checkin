#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全自动任务一键入口"""
import sys
import os

# 把项目根目录加入路径
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from orchestrator.run_all import run_all

if __name__ == "__main__":
    run_all()
