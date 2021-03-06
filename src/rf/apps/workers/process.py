# -*- coding: utf-8 -*-
from __future__ import print_function
from __future__ import unicode_literals
from __future__ import division

import math
import time
import logging

from django.conf import settings

from apps.core.models import LayerImage, Layer
from apps.workers.image_validator import ImageValidator
from apps.workers.image_metadata import calculate_and_save_layer_boundingbox
from apps.workers.sqs_manager import SQSManager
from apps.workers.emr import create_cluster, cluster_is_alive
from apps.workers.thumbnail import thumbnail_layer, thumbnail_image
import apps.core.enums as enums
import apps.workers.status_updates as status_updates
from apps.workers.copy_images import s3_copy

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = (60 * 60 * 5)  # 5 Hours.

JOB_COPY_IMAGE = 'copy_image'
JOB_IMAGE_ARRIVAL = [
    'ObjectCreated:CompleteMultipartUpload',
    'ObjectCreated:Put',
    'ObjectCreated:Post',
    'ObjectCreated:Copy'
]
JOB_VALIDATE = 'validate'
JOB_THUMBNAIL_IMAGE = 'thumbnail_image'
JOB_THUMBNAIL_LAYER = 'thumbnail_layer'
JOB_HANDOFF = 'emr_handoff'
JOB_TIMEOUT = 'timeout'
JOB_HEARTBEAT = 'emr_heartbeat'
JOB_CHUNK = 'chunk'
JOB_MOSAIC = 'mosaic'

STARTED = 'STARTED'
FINISHED = 'FINISHED'
FAILED = 'FAILED'

MAX_WAIT = 20  # Seconds.
TIMEOUT_DELAY = 300

# Error messages
ERROR_MESSAGE_THUMBNAIL_FAILED = 'Thumbnail generation failed.'
ERROR_MESSAGE_IMAGE_TRANSFER = 'Transferring image failed.'
ERROR_MESSAGE_JOB_TIMEOUT = 'Layer could not be processed. ' + \
                            'The job timed out after ' + \
                            str(math.ceil(TIMEOUT_SECONDS / 60)) + ' minutes.'
ERROR_MESSAGE_EMR_DEAD = 'Processing failed.'
ERROR_MESSAGE_UNSUPPORTED_MIME = 'Only GeoTiffs are supported.'
ERROR_MESSAGE_IMAGE_NOT_VALID = 'Invalid GeoTiff file.'
ERROR_MESSAGE_IMAGE_TOO_FEW_BANDS = 'Image does not have at least 3 bands.'


class QueueProcessor(object):
    """
    Encapsulates behavior for connecting to an SQS queue and moving image data
    through the processing pipeline by adding new messages to the same queue.
    """
    def __init__(self):
        self.queue = SQSManager()

    def start_polling(self):
        while True:
            message = self.queue.get_message()
            if not message:
                continue

            record = message['payload']

            # Flatten S3 event.
            if 'Records' in record and record['Records'][0]:
                record = record['Records'][0]

            delete_message = self.handle_message(record)
            if delete_message:
                self.queue.remove_message(message)

    def handle_message(self, record):
        # Parse record fields.
        if self.is_test_event(record):
            return True

        try:
            if 'eventName' in record:
                job_type = record['eventName']
                event_source = record['eventSource']
            elif 'stage' in record:
                job_type = record['stage']
            else:
                raise KeyError('eventName and stage missing')
        except KeyError:
            log.exception('Missing fields in message %s', record)
            return False

        log.info('Executing job %s', job_type)

        if job_type == JOB_THUMBNAIL_IMAGE:
            return self.thumbnail_image(record)
        elif job_type == JOB_THUMBNAIL_LAYER:
            return self.thumbnail_layer(record)
        elif job_type == JOB_HANDOFF:
            return self.emr_hand_off(record)
        elif job_type == JOB_HEARTBEAT:
            return self.emr_heartbeat(record)
        elif job_type == JOB_TIMEOUT:
            return self.check_timeout(record)
        elif job_type in JOB_IMAGE_ARRIVAL:
            if event_source == 'aws:s3':
                return self.image_arrival(record)
        elif job_type == JOB_VALIDATE:
            return self.validate_image(record)
        elif job_type == JOB_COPY_IMAGE:
            return self.copy_image(record)
        elif job_type == JOB_CHUNK:
            return self.chunk(record)
        elif job_type == JOB_MOSAIC:
            return self.mosaic(record)

        return False

    def is_test_event(self, record):
        return 'Event' in record and record['Event'] == 's3:TestEvent'

    def image_arrival(self, record):
        """
        When an image arrives via post or copy, initiate a validation job.
        record -- attribute data from SQS.
        """
        try:
            key = record['s3']['object']['key']
        except KeyError:
            return False

        s3_uuid = extract_uuid_from_aws_key(key)

        try:
            image = LayerImage.objects.get(s3_uuid=s3_uuid)
        except:
            log.info('Ignoring thumbnail %s', s3_uuid)
            return True

        log.info('Image %d arrived', image.id)

        # This may have already been done by copy_image, but it's safe to
        # do it again.
        status_updates.mark_image_transfer_end(s3_uuid)

        data = {'image_id': image.id}
        self.queue.add_message(JOB_VALIDATE, data)
        return True

    def validate_image(self, record):
        """
        Use Gdal to verify an image is properly formatted and can be further
        processed for images that were just uploaded or copied to the S3
        bucket.
        record -- attribute data from SQS.
        """
        try:
            data = record['data']
            image_id = data['image_id']
        except KeyError:
            return False

        try:
            image_id = int(image_id)
        except ValueError:
            return False

        try:
            image = LayerImage.objects.get(id=image_id)
        except LayerImage.DoesNotExist:
            return False

        s3_uuid = image.s3_uuid
        key = image.get_s3_key()

        if image.has_been_validated():
            log.info('Image %d is already validated', image.id)
            return True

        # Validate image.
        status_updates.mark_image_status_start(s3_uuid, enums.STATUS_VALIDATE)
        validator = ImageValidator(settings.AWS_BUCKET_NAME, key)
        try:
            if not validator.image_format_is_supported():
                log.info('Image format not supported %s', key)
                return status_updates.mark_image_invalid(
                    s3_uuid, ERROR_MESSAGE_UNSUPPORTED_MIME)
            elif not validator.image_has_enough_bands():
                log.info('Not enough bands %s', key)
                return status_updates.mark_image_invalid(
                    s3_uuid, ERROR_MESSAGE_IMAGE_TOO_FEW_BANDS)
        except RuntimeError:
            log.exception('GDAL was unable to open %s', key)
            return status_updates.mark_image_invalid(
                s3_uuid, ERROR_MESSAGE_IMAGE_NOT_VALID)

        updated = status_updates.mark_image_valid(s3_uuid)
        if updated:
            log.info('Image validated %s', key)

            log.info('Getting metadata from image %s', key)
            bounds = validator.get_image_bounds()
            image.set_bounds(bounds)

            layer_id = status_updates.get_layer_id_from_uuid(s3_uuid)
            layer_images = LayerImage.objects.filter(layer_id=layer_id)
            # TODO: Filter `layer_images` instead of extra query.
            valid_images = LayerImage.objects.filter(
                layer_id=layer_id,
                status_validate_end__isnull=False,
                status_validate_error__isnull=True)
            all_valid = len(layer_images) == len(valid_images)
            layer_has_images = len(layer_images) > 0

            log.info('%d/%d images validated for layer %d',
                     len(valid_images),
                     len(layer_images),
                     layer_id)

            if all_valid and layer_has_images:
                log.info('Layer %d is valid', layer_id)
                status_updates.mark_layer_status_end(
                    layer_id,
                    enums.STATUS_VALIDATE)

                data = {'layer_id': layer_id}

                log.info('Queue handoff job')
                self.queue.add_message(JOB_HANDOFF, data)

                log.info('Queue thumbnail jobs')
                for image in layer_images:
                    data = {'image_id': image.id}
                    self.queue.add_message(JOB_THUMBNAIL_IMAGE, data)
                data = {'layer_id': layer_id}
                self.queue.add_message(JOB_THUMBNAIL_LAYER, data)

                # Add a message to the queue to watch for timeouts.
                data = {
                    'timeout': time.time() + TIMEOUT_SECONDS,
                    'layer_id': layer_id
                }
                log.info('Queue timeout job')
                self.queue.add_message(JOB_TIMEOUT, data, TIMEOUT_DELAY)
            return True
        return False

    def thumbnail_image(self, record):
        """
        Generate thumbnails for an image.
        record -- attribute data from SQS.
        """
        try:
            data = record['data']
            image_id = data['image_id']
        except KeyError:
            return False

        try:
            image_id = int(image_id)
        except ValueError:
            return False

        image = LayerImage.objects.get(id=image_id)
        layer_id = image.layer.id
        log.info('Generating thumbnails for image %d...', image_id)
        status_updates.mark_image_status_start(image.s3_uuid,
                                               enums.STATUS_THUMBNAIL)

        try:
            thumbnail_image(image_id)
            status_updates.mark_image_status_end(image.s3_uuid,
                                                 enums.STATUS_THUMBNAIL)
            log.info('Done generating thumbnails for image %d', image_id)
            status_updates.mark_layer_thumbnailed(layer_id)
        except:
            log.exception('Failed to thumbnail image %d', image_id)
            status_updates.mark_image_status_end(
                image_id,
                enums.STATUS_THUMBNAIL,
                ERROR_MESSAGE_THUMBNAIL_FAILED)
        return True

    def thumbnail_layer(self, record):
        """
        Generate thumbnails for a layer.
        record -- attribute data from SQS.
        """
        try:
            data = record['data']
            layer_id = data['layer_id']
        except KeyError:
            return False

        try:
            layer_id = int(layer_id)
        except ValueError:
            return False

        log.info('Processing bounds for layer %d...', layer_id)
        calculate_and_save_layer_boundingbox(layer_id)

        log.info('Generating thumbnails for layer %d...', layer_id)
        status_updates.mark_layer_status_start(layer_id,
                                               enums.STATUS_THUMBNAIL)

        try:
            thumbnail_layer(layer_id)
            status_updates.mark_layer_thumbnailed(layer_id)
            log.info('Done generating thumbnails for layer %d', layer_id)
        except:
            log.exception('Failed to thumbnail layer %d', layer_id)
            status_updates.mark_layer_status_end(
                layer_id,
                enums.STATUS_THUMBNAIL,
                ERROR_MESSAGE_THUMBNAIL_FAILED)
        return True

    def emr_hand_off(self, record):
        """
        Passes layer imgages to EMR to begin creating custom rasters.
        record -- attribute data from SQS.
        """
        try:
            data = record['data']
            layer_id = data['layer_id']
        except KeyError:
            return False

        try:
            layer_id = int(layer_id)
        except ValueError:
            return False

        layer = Layer.objects.get(id=layer_id)
        status_updates.mark_layer_status_start(layer.id,
                                               enums.STATUS_CREATE_CLUSTER)
        log.info('Launching EMR cluster for layer %d', layer_id)

        emr_response = create_cluster(layer)
        self.start_health_check(layer_id, emr_response)
        return True

    def emr_heartbeat(self, record):
        try:
            data = record['data']
            job_id = data['job_id']
            layer_id = data['layer_id']
        except KeyError:
            return False

        try:
            layer_id = int(layer_id)
        except ValueError:
            return False

        try:
            layer = Layer.objects.get(id=layer_id)
        except Layer.DoesNotExist:
            return False

        log.debug('Heartbeat for layer %d', layer_id)

        if layer.status_completed or layer.status_failed:
            log.debug('Ending heartbeat. Job is complete.')
            return True
        elif not cluster_is_alive(job_id):
            if layer.status_heartbeat is not None:
                log.info('Second failed EMR heartbeat for layer %d!', layer_id)
                log.info('EMR job for layer %d has failed!', layer_id)
                layer.process_failed_heartbeat()
                return status_updates.mark_layer_status_end(
                    layer_id, enums.STATUS_FAILED, ERROR_MESSAGE_EMR_DEAD)
            else:
                log.info('EMR Heartbeat failed for layer %d.', layer_id)
                layer.process_failed_heartbeat()

        # If job is not failed or completed add another heartbeat message.
        data = {'layer_id': layer_id, 'job_id': job_id}
        self.queue.add_message(JOB_HEARTBEAT, data, TIMEOUT_DELAY)
        return True

    def check_timeout(self, record):
        """
        Check an EMR job to see if it has completed before the timeout has
        been reached.
        record -- attribute data from SQS.
        """
        try:
            data = record['data']
            layer_id = data['layer_id']
            timeout = data['timeout']
        except KeyError:
            # A bad message showed up. Leave it alone. Eventually it'll end
            # in the dead letter queue.
            return False

        try:
            layer_id = int(layer_id)
            timeout = float(timeout)
        except ValueError:
            return False

        try:
            layer = Layer.objects.get(id=layer_id)
        except Layer.DoesNotExist:
            return True

        log.debug('Timeout for layer %d', layer_id)

        if layer.status_completed or layer.status_failed:
            log.debug('Ending timeout. Job is complete.')
            return True

        if time.time() > timeout:
            log.info('Layer %d timed out!', layer_id)
            return status_updates.mark_layer_status_end(
                layer_id,
                enums.STATUS_FAILED,
                ERROR_MESSAGE_JOB_TIMEOUT)

        # Requeue the timeout message.
        self.queue.add_message(JOB_TIMEOUT, data, TIMEOUT_DELAY)
        return True

    def copy_image(self, record):
        """
        Copy an image into the S3 bucket from an external source.
        data -- attribute data from SQS.
        """
        try:
            data = record['data']
            image_id = data['image_id']
        except KeyError:
            return False

        try:
            image_id = int(image_id)
        except ValueError:
            return False

        try:
            image = LayerImage.objects.get(id=image_id)
        except LayerImage.DoesNotExist:
            return False

        log.info('Copying %s to %s...',
                 image.source_s3_bucket_key,
                 image.get_s3_key())

        # Note start of task.
        status_updates.mark_image_transfer_start(image.s3_uuid)

        if image.source_s3_bucket_key:
            success = s3_copy(image.source_s3_bucket_key, image.get_s3_key())
            if success:
                log.info('Image copied successfully')
                return status_updates.mark_image_transfer_end(image.s3_uuid)
            else:
                log.info('Could not copy image')
                return status_updates.mark_image_transfer_failure(
                    image.s3_uuid, ERROR_MESSAGE_IMAGE_TRANSFER)
        else:
            return status_updates.mark_image_transfer_failure(
                image.s3_uuid, ERROR_MESSAGE_IMAGE_TRANSFER)

    def emr_event(self, record, step_name, emr_status):
        """
        Update status of layer based on EMR events.
        """
        try:
            layer_id = record['jobId']
            status = record['status']
        except KeyError:
            return False

        try:
            layer_id = int(layer_id)
        except ValueError:
            return False

        log.info('%s layer %d %s', step_name, layer_id, status)

        if status == FAILED:
            default_error = emr_status + ' job failed.'
            error = record.get('error', default_error)
            if error.strip() == '':
                error = default_error
            return status_updates.mark_layer_status_end(layer_id,
                                                        emr_status,
                                                        error)
        elif status == STARTED:
            # If chunking started then the cluster creation succeeded.
            if emr_status == enums.STATUS_CHUNK:
                status_updates.mark_layer_status_end(
                    layer_id, enums.STATUS_CREATE_CLUSTER)

            return status_updates.mark_layer_status_start(layer_id, emr_status)
        elif status == FINISHED:
            if emr_status == enums.STATUS_MOSAIC:
                status_updates.mark_layer_status_end(layer_id,
                                                     enums.STATUS_COMPLETED)

            return status_updates.mark_layer_status_end(layer_id, emr_status)
        else:
            return False

    def chunk(self, record):
        """
        Update status of layer based on chunk events.
        """
        return self.emr_event(record, 'Chunk', enums.STATUS_CHUNK)

    def mosaic(self, record):
        """
        Update status of layer based on mosaic events.
        """
        return self.emr_event(record, 'Mosaic', enums.STATUS_MOSAIC)

    def start_health_check(self, layer_id, emr_response):
        try:
            job_id = emr_response['JobFlowId']
        except KeyError:
            return False

        data = {'job_id': job_id, 'layer_id': layer_id}
        self.queue.add_message(JOB_HEARTBEAT, data, TIMEOUT_DELAY)


def extract_uuid_from_aws_key(key):
    """
    Given an AWS key, find the uuid and return it.
    AWS keys are a user id appended to a uuid with a file extension.
    EX: 10-1aa064aa-1086-4ff1-a90b-09d3420e0343.tif
    """
    dot = key.rfind('.') if key.rfind('.') >= 0 else len(key)
    first_dash = key.find('-')
    return key[first_dash + 1:dot]
