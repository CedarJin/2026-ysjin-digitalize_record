"""Export recorded extraction durations without contacting the model provider."""
import argparse
import csv
import io
import os
import time
from pathlib import Path

from sqlalchemy import select

from food_record_digitizer.db import ExtractionRun, Record
from food_record_digitizer.workspace import Workspace


def write_csv(path, rows, fields):
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(buffer.getvalue(), encoding='utf-8-sig')
    temporary.replace(path)


def export(workspace, output):
    output.mkdir(parents=True, exist_ok=True)
    runs, summaries = [], []
    with workspace.factory() as session:
        records = list(session.scalars(select(Record).where(
            Record.project_id == workspace.project_id).order_by(Record.record_id)))
        for record in records:
            history = list(session.scalars(select(ExtractionRun).where(
                ExtractionRun.record_pk == record.id).order_by(ExtractionRun.created_at)))
            durations, successes = [], []
            for run in history:
                seconds = float(run.elapsed_seconds) if run.elapsed_seconds is not None else None
                if seconds is not None:
                    durations.append(seconds)
                if run.status == 'succeeded':
                    successes.append(run)
                runs.append(dict(record_id=record.record_id, input_version=run.input_version,
                    run_id=run.id, status=run.status, started_at_utc=run.created_at,
                    finished_at_utc=run.finished_at or '', elapsed_seconds=seconds if seconds is not None else '',
                    elapsed_minutes=round(seconds / 60, 3) if seconds is not None else '',
                    error_code=run.error_code or '', model=run.configuration.get('model', ''),
                    provider_attempts=len(run.configuration.get('attempts', []))))
            last = successes[-1] if successes else None
            summaries.append(dict(record_id=record.record_id, current_state=record.state,
                run_count=len(history), successful_runs=len(successes),
                failed_runs=sum(r.status == 'failed' for r in history),
                running_runs=sum(r.status == 'running' for r in history),
                missing_duration_runs=sum(r.elapsed_seconds is None for r in history),
                recorded_total_seconds=round(sum(durations), 3) if durations else '',
                recorded_total_minutes=round(sum(durations) / 60, 3) if durations else '',
                latest_success_seconds=last.elapsed_seconds if last else '',
                latest_success_input_version=last.input_version if last else ''))
    run_fields = ['record_id','input_version','run_id','status','started_at_utc','finished_at_utc',
                  'elapsed_seconds','elapsed_minutes','error_code','model','provider_attempts']
    summary_fields = ['record_id','current_state','run_count','successful_runs','failed_runs',
                      'running_runs','missing_duration_runs','recorded_total_seconds',
                      'recorded_total_minutes','latest_success_seconds','latest_success_input_version']
    write_csv(output / 'extraction-runs.csv', runs, run_fields)
    write_csv(output / 'record-timings.csv', summaries, summary_fields)
    return len(runs), len(summaries)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--watch-pid', type=int)
    args = parser.parse_args()
    workspace = Workspace(args.workspace)
    while True:
        counts = export(workspace, args.output)
        if not args.watch_pid:
            print(f'Exported {counts[0]} runs for {counts[1]} records.')
            return
        try:
            os.kill(args.watch_pid, 0)
        except ProcessLookupError:
            export(workspace, args.output)
            return
        time.sleep(60)


if __name__ == '__main__':
    main()
