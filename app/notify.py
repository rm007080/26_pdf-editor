def show_notification(title: str, body: str) -> None:
    from winotify import Notification

    Notification(app_id="PDF編集", title=title, msg=body, duration="short").show()
