from motorcal.refresh import build_scheduler


def test_build_scheduler_registers_the_reload_job():
    def reload_job():
        pass

    jobs = build_scheduler(reload_job, reload_interval_seconds=30).get_jobs()

    assert [job.func for job in jobs] == [reload_job]


def test_build_scheduler_does_not_start_automatically():
    assert build_scheduler(lambda: None).running is False
