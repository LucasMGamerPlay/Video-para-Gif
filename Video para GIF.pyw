# -*- coding: utf-8 -*-
"""Atalho para abrir o conversor com duplo clique (sem janela de console).

Como a extensao .pyw esta associada ao pyw.exe do Windows, este arquivo abre
direto a interface. Voce tambem pode arrastar um video para cima dele.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from video_para_gif import main

main()
