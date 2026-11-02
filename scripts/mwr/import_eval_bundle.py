import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from scripts.mwr.bundle_tools import import_archive,audit_bundle


def main():
    p=argparse.ArgumentParser(description='Verify and import a separate test/gold bundle, without model calls.')
    p.add_argument('--archive',type=Path,required=True);p.add_argument('--destination',type=Path,required=True)
    p.add_argument('--development',type=Path,required=True);p.add_argument('--report-dir',type=Path,required=True)
    a=p.parse_args();integrity=import_archive(a.archive,a.destination);report,maps=audit_bundle(a.destination,a.development)
    report['integrity']=integrity;report['bundle_path']=str(a.destination.resolve())
    a.report_dir.mkdir(parents=True,exist_ok=True)
    (a.report_dir/'audit.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    (a.report_dir/'support_document_map.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in maps))
    print(json.dumps({k:report[k] for k in ['sample_count','gold_count','type_counts','overlap_with_development','support_fact_count','support_document_count']},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
