"""
handle images referenced by notes in mongodb
"""

import config

import io
from ftplib import FTP

import logging
logger = logging.getLogger("en2mongo.imagehandler")


class ImageHandler:

    def __init__(self):
        self.ftp = FTP(config.FTP_HOST)
        self.ftp.login(config.FTP_USER, config.FTP_PWD)

    def upload_image(self, img_dir, img_name, img_data):
        logger.debug("prepare image upload to %s", img_dir)
        img_dir = self._prepare_upload_target(self.ftp, img_dir)
        logger.debug("upload image '%s' to '%s'", img_name, img_dir)
        fp = io.BytesIO(img_data)
        img_path = "%s/%s" % (img_dir, img_name)
        self.ftp.storbinary("STOR %s" % img_path, fp)
        return img_path

    def _prepare_upload_target(self, ftp, img_dir):
        if not img_dir.startswith('files/'):
            img_dir = 'files/' + img_dir
        if not img_dir.endswith('/'):
            img_dir += '/'

        create_steps = []
        dir_path = img_dir
        retry = 5
        while 1:
            try:
                while not ftp.nlst(dir_path):
                    create_steps.append(dir_path)
                    dir_path = dir_path[:-1]  # strip trailing slash
                    assert '/' in dir_path
                    dir_path = dir_path[:dir_path.rfind('/') + 1]

                while create_steps:
                    dir_path = create_steps.pop()
                    try:
                        ftp.mkd(dir_path)
                    except Exception as err:
                        # error_perm('550 Create directory operation failed.',)
                        # happens under not yet determined circumstances, although directory is created successfully
                        # double-check to avoid false positive errors
                        pardir = '/'.join(dir_path.rsplit('/')[:-2])
                        subdir = dir_path.rsplit('/')[-2]
                        test = [d for d in ftp.nlst(pardir) if d.endswith('/' + subdir)]
                        if test:
                            logger.warning("ftp reported error creating %s, but exists", dir_path)
                            continue
                        logger.error("ftp.mkd failed for %s %s", dir_path, err)
                        retry = 0
                        raise RuntimeError("failed create directory %s to upload image " % dir_path)
                    else:
                        # logger.debug('created ftp dir %s', dir_path)
                        pass

            except Exception as err:
                # 421 Timeout, after retry / login:
                # error(10053, 'Eine bestehende Verbindung wurde softwaregesteuert\r\ndurch den Hostcomputer abgebrochen')
                # failed to restore connectivity
                if retry > 0:
                    # session expired? unfortunately does not fix it
                    self.ftp = FTP(config.FTP_HOST)
                    self.ftp.login(config.FTP_USER, config.FTP_PWD)
                    retry -= 1
                else:
                    raise RuntimeError("failed to prepare upload target")
            else:
                break

        return img_dir
