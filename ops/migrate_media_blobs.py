"""One-time, explicit migration of legacy DB media BLOBs to MEDIA_ROOT.

Run only after the NAS bind has passed RW verification and after taking the
application/database rollback point. This script never runs automatically.
"""
from app import app, db, EventConfig, ManualDoc, write_media_file


def move():
    moved = 0
    with app.app_context():
        for cfg in EventConfig.query.all():
            if cfg.poster_data and not cfg.poster_path:
                cfg.poster_path = write_media_file(
                    f'posters/{cfg.pujan_year}',
                    cfg.poster_name or f'chopda-pujan-{cfg.pujan_year}.jpg',
                    cfg.poster_data,
                )
                cfg.poster_data = None
                moved += 1

        for doc in ManualDoc.query.all():
            if doc.data and not doc.file_path:
                doc.file_path = write_media_file('manuals', doc.filename or 'manual.pdf', doc.data)
                doc.data = None
                moved += 1
            if doc.data_gu and not doc.file_path_gu:
                doc.file_path_gu = write_media_file('manuals', doc.filename_gu or 'manual-gu.pdf', doc.data_gu)
                doc.data_gu = None
                moved += 1

        db.session.commit()
    print(f'Migrated {moved} media object(s) from DB BLOBs to MEDIA_ROOT')


if __name__ == '__main__':
    move()
