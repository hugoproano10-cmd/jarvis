#!/bin/bash
# Sincroniza DB y estado cripto a jarvis-brain cada 5 minutos
scp -q /home/hproano/asistente/datos/jarvis_history.db jarvis-brain:/home/hproano/dashboard/datos/
scp -q /home/hproano/asistente/cripto/estado_cripto.json jarvis-brain:/home/hproano/dashboard/cripto/
