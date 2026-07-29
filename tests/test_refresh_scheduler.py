from motorcal.refresh import build_scheduler


def test_build_scheduler_registers_the_refresh_cron_job():
    calls = {"refresh": 0, "reload": 0}

    def refresh_job():
        calls["refresh"] += 1

    def reload_job():
        calls["reload"] += 1

    scheduler = build_scheduler(refresh_job, "17 */6 * * *", reload_job, reload_interval_seconds=30)

    jobs = scheduler.get_jobs()
    assert len(jobs) == 2
    job_funcs = {job.func for job in jobs}
    assert refresh_job in job_funcs
    assert reload_job in job_funcs


def test_build_scheduler_does_not_start_automatically():
    scheduler = build_scheduler(lambda: None, "0 * * * *", lambda: None)
    assert scheduler.running is False
