"""Reading uploaded forms."""

def parse_multipart(content_type, body):
    """multipart/form-data -> (fields, files) where files[name] = (filename, content_type, bytes)."""
    from email import policy
    from email.parser import BytesParser
    msg = BytesParser(policy=policy.HTTP).parsebytes(b"Content-Type: " + content_type.encode("latin-1") + b"\r\n\r\n" + body)
    fields, files = {}, {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename is not None:
            files[name] = (filename, part.get_content_type(), payload)
        else:
            fields[name] = payload.decode("utf-8", "replace")
    return fields, files
