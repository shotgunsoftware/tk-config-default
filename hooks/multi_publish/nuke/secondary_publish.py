# Copyright (c) 2015 Shotgun Software Inc.
#
# CONFIDENTIAL AND PROPRIETARY
#
# This work is provided "AS IS" and subject to the Shotgun Pipeline Toolkit
# Source Code License included in this distribution package. See LICENSE.
# By accessing, using, copying or modifying this work you indicate your
# agreement to the Shotgun Pipeline Toolkit Source Code License. All rights
# not expressly granted therein are reserved by Shotgun Software Inc.

import os
import shutil
import codecs
import datetime
import shutil
import uuid
import nuke
import xml.dom.minidom as minidom

import tank
from tank import Hook
from tank import TankError

class PublishHook(Hook):
    """
    Single hook that implements publish functionality for secondary tasks
    """
    def __init__(self, *args, **kwargs):
        """
        Construction
        """
        # call base init
        Hook.__init__(self, *args, **kwargs)

        # cache a couple of apps that we may need later on:
        self.__write_node_app = self.parent.engine.apps.get("tk-nuke-writenode")
        self.__review_submission_app = self.parent.engine.apps.get("tk-multi-reviewsubmission")

    def execute(self, tasks, work_template, comment, thumbnail_path, sg_task, primary_task, primary_publish_path, progress_cb, **kwargs):
        """
        Main hook entry point
        :param tasks:                   List of secondary tasks to be published.  Each task is a
                                        dictionary containing the following keys:
                                        {
                                            item:   Dictionary
                                                    This is the item returned by the scan hook
                                                    {
                                                        name:           String
                                                        description:    String
                                                        type:           String
                                                        other_params:   Dictionary
                                                    }

                                            output: Dictionary
                                                    This is the output as defined in the configuration - the
                                                    primary output will always be named 'primary'
                                                    {
                                                        name:             String
                                                        publish_template: template
                                                        tank_type:        String
                                                    }
                                        }

        :param work_template:           template
                                        This is the template defined in the config that
                                        represents the current work file

        :param comment:                 String
                                        The comment provided for the publish

        :param thumbnail:               Path string
                                        The default thumbnail provided for the publish

        :param sg_task:                 Dictionary (shotgun entity description)
                                        The shotgun task to use for the publish

        :param primary_publish_path:    Path string
                                        This is the path of the primary published file as returned
                                        by the primary publish hook

        :param progress_cb:             Function
                                        A progress callback to log progress during pre-publish.  Call:

                                            progress_cb(percentage, msg)

                                        to report progress to the UI

        :param primary_task:            The primary task that was published by the primary publish hook.  Passed
                                        in here for reference.  This is a dictionary in the same format as the
                                        secondary tasks above.

        :returns:                       A list of any tasks that had problems that need to be reported
                                        in the UI.  Each item in the list should be a dictionary containing
                                        the following keys:
                                        {
                                            task:   Dictionary
                                                    This is the task that was passed into the hook and
                                                    should not be modified
                                                    {
                                                        item:...
                                                        output:...
                                                    }

                                            errors: List
                                                    A list of error messages (strings) to report
                                        }
        """
        results = []

        # it's important that tasks for render output are processed
        # before tasks for quicktime output, so let's group the
        # task list by output.  This can be controlled through the
        # configuration but we shouldn't rely on that being set up
        # correctly!
        output_order = ["render", "quicktime"]
        tasks_by_output = {}
        for task in tasks:
            output_name = task["output"]["name"]
            tasks_by_output.setdefault(output_name, list()).append(task)
            if output_name not in output_order:
                output_order.append(output_name)

        # make sure we have any apps required by the publish process:
        if "render" in tasks_by_output or "quicktime" in tasks_by_output:
            # we will need the write node app if we have any render outputs to validate
            if not self.__write_node_app:
                raise TankError("Unable to publish Shotgun Write Nodes without the tk-nuke-writenode app!")

        if "quicktime" in tasks_by_output:
            # If we have the tk-multi-reviewsubmission app we can create versions
            if not self.__review_submission_app:
                raise TankError("Unable to publish Review Versions without the tk-multi-reviewsubmission app!")


        # Keep of track of what has been published in shotgun
        # this is needed as input into the review creation code...
        render_publishes = {}

        # process outputs in order:
        for output_name in output_order:

            # process each task for this output:
            for task in tasks_by_output.get(output_name, []):

                # keep track of our errors for this task
                errors = []

                # report progress:
                progress_cb(0.0, "Publishing", task)

                if output_name == "render":
                    # Publish the rendered output for a Shotgun Write Node

                    # each publish task is connected to a nuke write node
                    # this value was populated via the scan scene hook
                    write_node = task["item"].get("other_params", dict()).get("node")
                    if not write_node:
                        raise TankError("Could not determine nuke write node for item '%s'!" % str(task))

                    # publish write-node rendered sequence
                    try:
                        (sg_publish, thumbnail_path) = self._publish_write_node_render(task,
                                                                                       write_node,
                                                                                       primary_publish_path,
                                                                                       sg_task,
                                                                                       comment,
                                                                                       progress_cb)

                        # keep track of our publish data so that we can pick it up later in review
                        render_publishes[ write_node.name() ] = (sg_publish, thumbnail_path)
                    except Exception, e:
                        errors.append("Publish failed - %s" % e)

                elif output_name == "quicktime":
                    # Publish the reviewable quicktime movie for a Shotgun Write Node

                    # each publish task is connected to a nuke write node
                    # this value was populated via the scan scene hook
                    write_node = task["item"].get("other_params", dict()).get("node")
                    if not write_node:
                        raise TankError("Could not determine nuke write node for item '%s'!" % str(task))

                    # Submit published sequence to Screening Room
                    try:
                        # pick up sg data from the render dict we are maintianing
                        # note: we assume that the rendering tasks always happen
                        # before the review tasks inside the publish...
                        (sg_publish, thumbnail_path) = render_publishes[ write_node.name() ]

                        self._send_to_screening_room (
                            write_node,
                            sg_publish,
                            sg_task,
                            comment,
                            thumbnail_path,
                            progress_cb
                        )

                    except Exception, e:
                        errors.append("Submit to Screening Room failed - %s" % e)

                elif output_name == "flame":
                    # Update the Flame clip xml

                    # each publish task is connected to a nuke write node
                    # this value was populated via the scan scene hook
                    write_node = task["item"].get("other_params", dict()).get("node")
                    if not write_node:
                        raise TankError("Could not determine nuke write node for item '%s'!" % str(task))

                    # update shot clip xml file with this publish
                    try:
                        # compute the location of the clip file
                        clip_template = task["output"]["publish_template"]
                        clip_fields = self.parent.context.as_template_fields(clip_template)
                        clip_path = clip_template.apply_fields(clip_fields)

                        # pick up sg data from the render dict we are maintaining
                        # note: we assume that the rendering tasks always happen
                        # before the Flame tasks inside the publish...
                        (sg_publish, thumbnail_path) = render_publishes[ write_node.name() ]

                        self._update_flame_clip(clip_path, write_node, sg_publish, progress_cb)

                    except Exception, e:
                        errors.append("Could not update Flame clip xml: %s" % e)
                        # log the full call stack in addition to showing the error in the UI.
                        self.parent.log_exception("Could not update Flame clip xml!")


                else:
                    # unhandled output type!
                    errors.append("Don't know how to publish this item!")

                # if there is anything to report then add to result
                if len(errors) > 0:
                    # add result:
                    results.append({"task":task, "errors":errors})

                # task is finished
                progress_cb(100)

        return results


    def _send_to_screening_room(self, write_node, sg_publish, sg_task, comment, thumbnail_path, progress_cb):
        """
        Take a write node's published files and run them through the review_submission app
        to get a movie and Shotgun Version.

        :param write_node:      The Shotgun Write node to submit a review version for
        :param sg_publish:      The Shotgun publish entity dictionary to link the version with
        :param sg_task:         The Shotgun task entity dictionary for the publish
        :param comment:         The publish comment
        :param thumbnail_path:  The path to a thumbnail for the publish
        :param progress_cb:     A callback to use to report any progress
        """
        render_path = self.__write_node_app.get_node_render_path(write_node)
        render_template = self.__write_node_app.get_node_render_template(write_node)
        publish_template = self.__write_node_app.get_node_publish_template(write_node)
        render_path_fields = render_template.get_fields(render_path)

        if hasattr(self.__review_submission_app, "render_and_submit_version"):
            # this is a recent version of the review submission app that contains
            # the new method that also accepts a colorspace argument.
            colorspace = self._get_node_colorspace(write_node)
            self.__review_submission_app.render_and_submit_version(
                publish_template,
                render_path_fields,
                int(nuke.root()["first_frame"].value()),
                int(nuke.root()["last_frame"].value()),
                [sg_publish],
                sg_task,
                comment,
                thumbnail_path,
                progress_cb,
                colorspace
            )
        else:
            # This is an older version of the app so fall back to the legacy
            # method - this may mean the colorspace of the rendered movie is
            # inconsistent/wrong!
            self.__review_submission_app.render_and_submit(
                publish_template,
                render_path_fields,
                int(nuke.root()["first_frame"].value()),
                int(nuke.root()["last_frame"].value()),
                [sg_publish],
                sg_task,
                comment,
                thumbnail_path,
                progress_cb
            )

    def _get_node_colorspace(self, node):
        """
        Get the colorspace for the specified nuke node

        :param node:    The nuke node to find the colorspace for
        :returns:       The string representing the colorspace for the node
        """
        cs_knob = node.knob("colorspace")
        if not cs_knob:
            return

        cs = cs_knob.value()
        # handle default value where cs would be something like: 'default (linear)'
        if cs.startswith("default (") and cs.endswith(")"):
            cs = cs[9:-1]
        return cs

    def _publish_write_node_render(self, task, write_node, published_script_path, sg_task, comment, progress_cb):
        """
        Publish render output for write node
        """

        if self.__write_node_app.is_node_render_path_locked(write_node):
            # this is a fatal error as publishing would result in inconsistent paths for the rendered files!
            raise TankError("The render path is currently locked and does not match match the current Work Area.")

        progress_cb(10, "Finding renders")

        # get info we need in order to do the publish:
        render_path = self.__write_node_app.get_node_render_path(write_node)
        render_files = self.__write_node_app.get_node_render_files(write_node)
        render_template = self.__write_node_app.get_node_render_template(write_node)
        publish_template = self.__write_node_app.get_node_publish_template(write_node)
        tank_type = self.__write_node_app.get_node_tank_type(write_node)

        # publish (copy files):

        progress_cb(25, "Copying files")

        for fi, rf in enumerate(render_files):

            progress_cb(25 + (50*(len(render_files)/(fi+1))))

            # construct the publish path:
            fields = render_template.get_fields(rf)
            fields["TankType"] = tank_type
            target_path = publish_template.apply_fields(fields)

            # copy the file
            try:
                target_folder = os.path.dirname(target_path)
                self.parent.ensure_folder_exists(target_folder)
                self.parent.copy_file(rf, target_path, task)
            except Exception, e:
                raise TankError("Failed to copy file from %s to %s - %s" % (rf, target_path, e))

        progress_cb(40, "Publishing to Shotgun")

        # use the render path to work out the publish 'file' and name:
        render_path_fields = render_template.get_fields(render_path)
        render_path_fields["TankType"] = tank_type
        publish_path = publish_template.apply_fields(render_path_fields)

        # construct publish name:
        publish_name = ""
        rp_name = render_path_fields.get("name")
        rp_channel = render_path_fields.get("channel")
        if not rp_name and not rp_channel:
            publish_name = "Publish"
        elif not rp_name:
            publish_name = "Channel %s" % rp_channel
        elif not rp_channel:
            publish_name = rp_name
        else:
            publish_name = "%s, Channel %s" % (rp_name, rp_channel)

        publish_version = render_path_fields["version"]

        # get/generate thumbnail:
        thumbnail_path = self.__write_node_app.generate_node_thumbnail(write_node)

        # register the publish:
        sg_publish = self._register_publish(publish_path,
                                            publish_name,
                                            sg_task,
                                            publish_version,
                                            tank_type,
                                            comment,
                                            thumbnail_path,
                                            [published_script_path])

        return sg_publish, thumbnail_path

    def _register_publish(self, path, name, sg_task, publish_version, tank_type, comment, thumbnail_path, dependency_paths):
        """
        Helper method to register publish using the
        specified publish info.
        """
        # construct args:
        args = {
            "tk": self.parent.tank,
            "context": self.parent.context,
            "comment": comment,
            "path": path,
            "name": name,
            "version_number": publish_version,
            "thumbnail_path": thumbnail_path,
            "task": sg_task,
            "dependency_paths": dependency_paths,
            "published_file_type":tank_type,
        }

        # register publish;
        sg_data = tank.util.register_publish(**args)

        return sg_data


    def _generate_flame_clip_name(self, publish_fields):
        """
        Generates a name which will be displayed in the dropdown in Flame.

        :param publish_fields: Publish fields
        :returns: name string
        """

        # this implementation generates names on the following form:
        #
        # Comp, scene.nk (output background), v023
        # Comp, Nuke, v023
        # Lighting CBBs, final.nk, v034
        #
        # (depending on what pieces are available in context and names, names may vary)

        name = ""

        # the shot will already be implied by the clip inside Flame (the clip file
        # which we are updating is a per-shot file. But if the context contains a task
        # or a step, we can display that:
        if self.parent.context.task:
            name += "%s, " % self.parent.context.task["name"].capitalize()
        elif self.parent.context.step:
            name += "%s, " % self.parent.context.step["name"].capitalize()

        # if we have a channel set for the write node
        # or a name for the scene, add those
        rp_name = publish_fields.get("name")
        rp_channel = publish_fields.get("channel")

        if rp_name and rp_channel:
            name += "%s.nk (output %s), " % (rp_name, rp_channel)
        elif not rp_name:
            name += "Nuke output %s, " % rp_channel
        elif not rp_channel:
            name += "%s.nk, " % rp_name
        else:
            name += "Nuke, "

        # and finish with version number
        name += "v%03d" % (publish_fields.get("version") or 0)

        return name

    def _update_flame_clip(self, clip_path, write_node, sg_publish, progress_cb):
        """
        Update the Flame open clip file for this shot with the published render.

        When a shot has been exported from flame, a clip file is available for each shot.
        We load that up, parse the xml and add a new entry to it.

        For docs on the clip format, see:
        http://knowledge.autodesk.com/support/flame-products/troubleshooting/caas/sfdcarticles/sfdcarticles/Creating-clip-Open-Clip-files-from-multi-EXR-assets.html
        http://docs.autodesk.com/flamepremium2015/index.html?url=files/GUID-1A051CEB-429B-413C-B6CA-256F4BB5D254.htm,topicNumber=d30e45343


        When the clip file is updated, a new <version> tag and a new <feed> tag are inserted:

        <feed type="feed" vuid="v002" uid="DA62F3A2-BA3B-4939-8089-EC7FC603AC74">
            <spans type="spans" version="4">
                <span type="span" version="4">
                    <path encoding="pattern">/nuke/publish/path/mi001_scene_output_v001.[0100-0150].dpx</path>
                </span>
            </spans>
        </feed>

        <version type="version" uid="v002">
            <name>Comp, scene.nk, v003</name>
            <creationDate>2014/12/09 22:30:04</creationDate>
            <userData type="dict">
            </userData>
        </version>

        An example clip XML file would look something like this:

        <?xml version="1.0" encoding="UTF-8"?>
        <clip type="clip" version="4">
            <handler type="handler">
                ...
            </handler>
            <name type="string">mi001</name>
            <sourceName type="string">F004_C003_0228F8</sourceName>
            <userData type="dict">
                ...
            </userData>
            <tracks type="tracks">
                <track type="track" uid="video">
                    <trackType>video</trackType>
                    <dropMode type="string">NDF</dropMode>
                    <duration type="time" label="00:00:07+02">
                        <rate type="rate">
                            <numerator>24000</numerator>
                            <denominator>1001</denominator>
                        </rate>
                        <nbTicks>170</nbTicks>
                        <dropMode>NDF</dropMode>
                    </duration>
                    <name type="string">mi001</name>
                    <userData type="dict">
                        <GATEWAY_NODE_ID type="binary">/mnt/projects/arizona_adventure/sequences/Mirage/mi001/editorial/flame/mi001.clip@TRACK(5)video</GATEWAY_NODE_ID>
                        <GATEWAY_SERVER_ID type="binary">10.0.1.8:Gateway</GATEWAY_SERVER_ID>
                        <GATEWAY_SERVER_NAME type="string">xxx</GATEWAY_SERVER_NAME>
                    </userData>
                    <feeds currentVersion="v002">

                        <feed type="feed" vuid="v000" uid="5E21801C-41C2-4B47-90B6-C1E25235F032">
                            <storageFormat type="format">
                                <type>video</type>
                                <channelsDepth type="uint">10</channelsDepth>
                                <channelsEncoding type="string">Integer</channelsEncoding>
                                <channelsEndianess type="string">Big Endian</channelsEndianess>
                                <fieldDominance type="int">2</fieldDominance>
                                <height type="uint">1080</height>
                                <nbChannels type="uint">3</nbChannels>
                                <pixelLayout type="string">RGB</pixelLayout>
                                <pixelRatio type="float">1</pixelRatio>
                                <width type="uint">1920</width>
                            </storageFormat>
                            <sampleRate type="rate" version="4">
                                <numerator>24000</numerator>
                                <denominator>1001</denominator>
                            </sampleRate>
                            <spans type="spans" version="4">
                                <span type="span" version="4">
                                    <duration>170</duration>
                                    <path encoding="pattern">/mnt/projects/arizona_adventure/sequences/Mirage/mi001/editorial/dpx_plates/v000/F004_C003_0228F8/F004_C003_0228F8_mi001.v000.[0100-0269].dpx</path>
                                </span>
                            </spans>
                        </feed>
                        <feed type="feed" vuid="v001" uid="DA62F3A2-BA3B-4939-8089-EC7FC602AC74">
                            <storageFormat type="format">
                                <type>video</type>
                                <channelsDepth type="uint">10</channelsDepth>
                                <channelsEncoding type="string">Integer</channelsEncoding>
                                <channelsEndianess type="string">Little Endian</channelsEndianess>
                                <fieldDominance type="int">2</fieldDominance>
                                <height type="uint">1080</height>
                                <nbChannels type="uint">3</nbChannels>
                                <pixelLayout type="string">RGB</pixelLayout>
                                <pixelRatio type="float">1</pixelRatio>
                                <rowOrdering type="string">down</rowOrdering>
                                <width type="uint">1920</width>
                            </storageFormat>
                            <userData type="dict">
                                <recordTimecode type="time" label="00:00:00+00">
                                    <rate type="rate">24</rate>
                                    <nbTicks>0</nbTicks>
                                    <dropMode>NDF</dropMode>
                                </recordTimecode>
                            </userData>
                            <sampleRate type="rate" version="4">
                                <numerator>24000</numerator>
                                <denominator>1001</denominator>
                            </sampleRate>
                            <startTimecode type="time">
                                <rate type="rate">24</rate>
                                <nbTicks>1391414</nbTicks>
                                <dropMode>NDF</dropMode>
                            </startTimecode>
                            <spans type="spans" version="4">
                                <span type="span" version="4">
                                    <path encoding="pattern">/mnt/projects/arizona_adventure/sequences/Mirage/mi001/editorial/dpx_plates/v001/F004_C003_0228F8/F004_C003_0228F8_mi001.v001.[0100-0269].dpx</path>
                                </span>
                            </spans>
                        </feed>
                    </feeds>
                </track>
            </tracks>
            <versions type="versions" currentVersion="v002">
                <version type="version" uid="v000">
                    <name>v000</name>
                    <creationDate>2014/12/09 22:22:48</creationDate>
                    <userData type="dict">
                        <batchSetup type="binary">/mnt/projects/arizona_adventure/sequences/Mirage/mi001/editorial/flame/batch/mi001.v000.batch</batchSetup>
                        <versionNumber type="uint64">0</versionNumber>
                    </userData>
                </version>
                <version type="version" uid="v001">
                    <name>v001</name>
                    <creationDate>2014/12/09 22:30:04</creationDate>
                    <userData type="dict">
                        <batchSetup type="binary">/mnt/projects/arizona_adventure/sequences/Mirage/mi001/editorial/flame/batch/mi001.v001.batch</batchSetup>
                        <versionNumber type="uint64">1</versionNumber>
                    </userData>
                </version>
            </versions>
        </clip>

        :param clip_path: path to the clip xml file to add the publish to
        :param write_node: current write node object
        :param sg_publish: shotgun publish
        :param progress_cb: progress callback
        """

        progress_cb(1, "Updating Flame clip file...")

        # get the fields from the work file
        render_path = self.__write_node_app.get_node_render_path(write_node)
        render_template = self.__write_node_app.get_node_render_template(write_node)
        render_path_fields = render_template.get_fields(render_path)
        publish_template = self.__write_node_app.get_node_publish_template(write_node)

        # append extra fields needed by the publish template
        tank_type = self.__write_node_app.get_node_tank_type(write_node)
        render_path_fields["TankType"] = tank_type

        # set up the sequence token to be Flame friendly
        # e.g. mi001_scene_output_v001.[0100-0150].dpx
        # note - we cannot take the frame ranges from the write node -
        # because those values indicate the intended frame range rather
        # than the rendered frame range! In order for Flame to pick up
        # the media properly, it needs to contain the actual frame data

        # get all paths for all frames and all eyes
        paths = self.parent.sgtk.paths_from_template(publish_template, render_path_fields, skip_keys = ["SEQ", "eye"])

        # for each of them, extract the frame number. Track the min and the max
        min_frame = None
        max_frame = None
        for path in paths:
            fields = publish_template.get_fields(path)
            frame_number = fields["SEQ"]
            if min_frame is None or frame_number < min_frame:
                min_frame = frame_number
            if max_frame is None or frame_number > max_frame:
                max_frame = frame_number

        if min_frame is None or max_frame is None:
            # shouldn't really end up here - the validation checks that
            # stuff has actually been rendered.
            raise TankError("Couldn't extract min and max frame from the published sequence! "
                            "Will not update Flame clip xml.")

        # now when we have the real min/max frame, we can apply a proper sequence marker for the
        # Flame xml. Note that we cannot use the normal FORMAT: token in the template system, because
        # the Flame frame format is not totally "abstract" (e.g. %04d, ####, etc) but contains the frame
        # ranges themselves.
        #
        # the format spec is something like "04"
        sequence_key = publish_template.keys["SEQ"]
        # now compose the format string, eg. [%04d-%04d]
        format_str = "[%%%sd-%%%sd]" % (sequence_key.format_spec, sequence_key.format_spec)
        # and lastly plug in the values
        render_path_fields["SEQ"] = format_str % (min_frame, max_frame)

        # contruct the final path - because flame doesn't have any windows support and
        # because the "hub" platform is always linux (with potential flame assist and flare
        # satellite setups on macosx), request that the paths are written out on linux form
        # regardless of the operating system currently running.
        publish_path_flame = publish_template.apply_fields(render_path_fields, "linux2")

        # open up and update our xml file
        xml = minidom.parse(clip_path)

        # find first <track type="track" uid="video">
        first_video_track = None
        for track in xml.getElementsByTagName("track"):
            if track.attributes["uid"].value == "video":
                first_video_track = track
                break

        if first_video_track is None:
            raise TankError("Could not find <track type='track' uid='video'> in clip file!")

        # now contruct our feed xml chunk we want to insert
        #
        # this is the xml structure we want to insert:
        #
        # <feed type="feed" vuid="%s" uid="%s">
        #     <spans type="spans" version="4">
        #         <span type="span" version="4">
        #             <path encoding="pattern">%s</path>
        #         </span>
        #     </spans>
        # </feed>
        unique_id = str(uuid.uuid4())

        # <feed type="feed" vuid="%s" uid="%s">
        feed_node = xml.createElement("feed")
        feed_node.setAttribute("type", "feed")
        feed_node.setAttribute("uid", unique_id)
        feed_node.setAttribute("vuid", unique_id)

        # <spans type="spans" version="4">
        spans_node = xml.createElement("spans")
        spans_node.setAttribute("type", "spans")
        spans_node.setAttribute("version", "4")
        feed_node.appendChild(spans_node)

        # <span type="span" version="4">
        span_node = xml.createElement("span")
        span_node.setAttribute("type", "span")
        span_node.setAttribute("version", "4")
        spans_node.appendChild(span_node)

        # <path encoding="pattern">%s</path>
        path_node = xml.createElement("path")
        path_node.setAttribute("encoding", "pattern")
        path_node.appendChild(xml.createTextNode(publish_path_flame))
        span_node.appendChild(path_node)

        # add new feed to first list of feeds inside of our track
        track.getElementsByTagName("feeds")[0].appendChild(feed_node)


        # now add same to the versions structure
        #
        # <version type="version" uid="%s">
        #     <name>%s</name>
        #     <creationDate>%s</creationDate>
        #     <userData type="dict">
        #     </userData>
        # </version>
        date_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        formatted_name = self._generate_flame_clip_name(render_path_fields)

        # <version type="version" uid="%s">
        version_node = xml.createElement("version")
        version_node.setAttribute("type", "version")
        version_node.setAttribute("uid", unique_id)

        # <name>v003 Comp</name>
        child_node = xml.createElement("name")
        child_node.appendChild(xml.createTextNode(formatted_name))
        version_node.appendChild(child_node)

        # <creationDate>1229-12-12 12:12:12</creationDate>
        child_node = xml.createElement("creationDate")
        child_node.appendChild(xml.createTextNode(date_str))
        version_node.appendChild(child_node)

        # <userData type="dict">
        child_node = xml.createElement("userData")
        child_node.setAttribute("type", "dict")
        version_node.appendChild(child_node)

        # add new feed to first list of versions
        xml.getElementsByTagName("versions")[0].appendChild(version_node)
        xml_string = xml.toxml(encoding="UTF-8")

        # make a backup of the clip file before we update it
        #
        # note - we are not using the template system here for simplicity
        # (user requiring customization can always modify this hook code themselves).
        # There is a potential edge case where the backup file cannot be written at this point
        # because you are on a different machine or running with different permissions.
        #
        backup_path = "%s.bak_%s" % (clip_path, datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
        try:
            shutil.copy(clip_path, backup_path)
        except Exception, e:
            raise TankError("Could not create backup copy of the Flame clip file '%s': %s" % (clip_path, e))

        fh = open(clip_path, "wt")
        try:
            fh.write(xml_string)
        finally:
            fh.close()
