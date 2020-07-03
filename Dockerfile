FROM python:3-slim

RUN mkdir -p /usr/src/app
WORKDIR /usr/src/app

COPY requirements.txt /usr/src/app
RUN pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org --no-cache-dir -r requirements.txt

COPY radosgw_exporter.py /usr/src/app

EXPOSE 9242
ENV RADOSGW_SERVER=http://radosgw:80 VIRTUAL_PORT=9242 DEBUG=0

ENTRYPOINT [ "python", "-u", "./radosgw_exporter.py" ]
CMD []
