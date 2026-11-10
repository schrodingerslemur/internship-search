"""Application assistant: prepare an application, fill the form, you submit.

Every Greenhouse, Lever and Ashby application form is protected by a bot check
(Greenhouse loads reCAPTCHA Enterprise on every board). This package therefore
never submits anything. It does everything up to the Submit button -- picks the
day's best matches, reads the form's questions from Greenhouse's public API,
answers them from your profile and resume, opens the form in a real browser
with every field filled and the resume attached -- and then waits for you to
review it and click Submit yourself.

Personal data stays on this machine: the applicant profile, drafts and browser
profile all live under ``data/apply/``, which git ignores. The repository is
public, so nothing here may ever be logged to CI or committed.
"""
