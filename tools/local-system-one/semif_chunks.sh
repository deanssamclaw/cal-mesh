#!/bin/bash
# Chunked SemIf scoring with a cool-down between chunks (jlab hits ~100 C under load).
cd ~/jev-eval || exit 1
GGUF=~/jev-eval/gguf/Qwen_Qwen3.5-4B-Q4_K_M.gguf
IN=semif_decisions.jsonl; OUT=semif_results.jsonl; CH=20
rm -f $OUT chunk_*.jsonl out_*.jsonl
split -l $CH -d -a 3 $IN chunk_
for c in chunk_*; do
  while [ "$(cat /sys/class/thermal/thermal_zone2/temp)" -gt 70000 ]; do sleep 5; done
  .venv/bin/semif-score --mode direct --backend llamacpp --gguf $GGUF --llama-threads 8 \
    --model Qwen/Qwen3.5-4B --revision 851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a \
    --input "$c" --output "out_$c.jsonl" >/dev/null 2>&1
  cat "out_$c.jsonl" >> $OUT
  echo "$(date -u +%T) $c done, total $(wc -l < $OUT) rows, temp $(( $(cat /sys/class/thermal/thermal_zone2/temp)/1000 ))C"
done
echo SEMIF_SCORE_DONE
