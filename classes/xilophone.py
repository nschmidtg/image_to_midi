from .image_analyzer import ImageAnalizer
from PIL import Image
from mido import Message
import threading
import numpy as np
import time
from . import settings
from .ramp import Ramp
c = threading.Condition()

scales = {
    "MAJOR": [0, 2, 4, 5, 7, 9, 11],
    "DORIAN": [0, 2, 3, 5, 7, 9, 10],
    "PHRYGIAN": [0, 1, 3, 5, 7, 8, 10],
    "LYDIAN": [0, 2, 4, 6, 7, 9, 11],
    "MIXOLYDIAN": [0, 2, 4, 5, 7, 9, 10],
    "MINOR": [0, 2, 3, 5, 7, 8, 10],
    "LOCRIAN": [0, 1, 3, 5, 6, 8, 10],
}


class XilophoneHandler():
    def __init__(self, image_path, max_channels, outport):
        self.image_path = image_path
        self.max_channels = max_channels
        self.outport = outport
        self.xilo_threads = []
        for i in range(self.max_channels):
            xilo = Xilophone(
                i,
                int(settings.params[f"CHANNEL-{i}"]) - 1,
                settings.params["IMAGE"],
                settings.params[f"SCALE-{i}"],
                int(settings.params[f"ROOT-{i}"]),
                int(settings.params[f"OCTAVES-{i}"]),
                self.outport,
                note_length=int(settings.params[f"DURATION-{i}"]),
                separation=int(settings.params[f"SEPARATION-{i}"]),
                uncompressed=settings.uncompressed[i],
                x_axis_direction=settings.params[f"DIRECTION-{i}"],
                intervals=settings.params[f"INPUTSCALE-{i}"]
            )
            xilo.start()
            self.xilo_threads.append(xilo)

    def xilo_lifecycle(self):
        current_n_people = 0
        while settings.keep_playing:
            initial_n_people = min(self.max_channels, settings.people_counter)

            # we only change the current n of xilos if the n of people changed
            # for more than 1 secs
            time.sleep(1)
            final_n_people = min(self.max_channels, settings.people_counter)

            if initial_n_people == final_n_people:
                if final_n_people != current_n_people:
                    # silence all xilos
                    for xilo in self.xilo_threads[final_n_people:]:
                        xilo.stop_thread()
                    for i in range(final_n_people):
                        self.xilo_threads[i].resume_thread()
                    current_n_people = final_n_people
        for xilo in self.xilo_threads:
            # xilo.stop_thread()
            xilo.join()


class Xilophone(threading.Thread):
    def __init__(
        self,
        index,
        midi_channel,
        image_path,
        scale,
        root_note,
        n_scales,
        outport,
        note_length=2000,
        separation=None,  # include it for polyphonic sounds
        uncompressed=False,
        x_axis_direction='left to right',
        intervals=None
    ):
        threading.Thread.__init__(self)
        self.index = index
        self.local_keep_playing = False
        self.poly = None
        if separation:
            self.poly = True
        self.uncompressed = uncompressed
        self.note_length = note_length
        self.midi_channel = midi_channel
        self.separation = separation
        if scale in scales.keys():
            selected_scale = scales[scale]
        elif scale == "CUSTOM":
            selected_scale = [int(note) for note in intervals.split(',')]
        self.x_axis_direction = x_axis_direction
        self.notes = []
        for i in range(n_scales):
            for note in selected_scale:
                self.notes.append(root_note + (12 * i) + note)
        max_velocity = 128
        self.current_time = 0
        self.n_notes = len(self.notes)
        image_a = ImageAnalizer()
        im = image_a.open(image_path)
        image = Image.Image.split(im)
        R = np.array(image[0])
        G = np.array(image[1])
        B = np.array(image[2])
        Grey = 0.299 * R + 0.587 * G + 0.114 * B
        W, H = Grey.shape
        delta_x = int(W/self.n_notes)
        delta_y = int(H/max_velocity)  # when using 2 synth

        # initialize prob dist
        prob_matrix = np.zeros(self.n_notes * max_velocity)
        self.notes_matrix = [None] * (self.n_notes * max_velocity)
        col_count = 0
        row_count = 0

        # populate prob dist based on white density on the image
        current = 0
        for col_count in range(0, self.n_notes):
            for row_count in range(0, max_velocity):
                self.notes_matrix[current] = "%s-%s" % (col_count, row_count)
                prob_matrix[current] = np.sum(Grey[
                    col_count * delta_x:(col_count + 1) * delta_x,
                    row_count * delta_y:(row_count + 1) * delta_y
                ])
                current += 1

        max_value = np.sum(prob_matrix)
        self.norm_probs = prob_matrix / max_value
        self.outport = outport

        # initialize midi CCs
        self.x_ramp = Ramp(
            self.outport,
            low=int(settings.params[f"MIN-{self.index}"]),
            high=int(settings.params[f"MAX-{self.index}"]),
            start=0,
            step=1,
            speed=5,
            channel=int(settings.params[f"CHANNEL-{self.index}"]) - 1,
            control=int(settings.params[f"CC-{self.index}"]),
            inst_num=self.index,
            direction=self.x_axis_direction)

    def stop_thread(self):
        self.local_keep_playing = False

    def resume_thread(self):
        self.local_keep_playing = True

    def send_note(self, note, duration, vel):
        msg = Message(
            'note_on',
            note=note,
            velocity=vel,
            channel=self.midi_channel
        )
        self.outport.send(msg)
        time.sleep(duration/1000)
        msg = Message(
            'note_off',
            note=note,
            channel=self.midi_channel
        )
        self.outport.send(msg)

    def run(self):
        play_note = None
        # read centroid
        self.x_ramp.start()
        while settings.keep_playing:
            if self.local_keep_playing:
                note_vel = np.random.choice(
                    self.notes_matrix,
                    p=self.norm_probs
                )
                pitch, volume = note_vel.split('-')
                pitch = int(self.notes[int(pitch)])
                volume = int(volume)
                if not self.uncompressed:
                    volume = 127
                time_sampled = max(0, np.random.normal(
                    loc=int(self.note_length),
                    scale=int(self.note_length/2)
                ))
                play_note = threading.Thread(
                    target=self.send_note,
                    args=(pitch, time_sampled, volume)
                )
                play_note.start()
                if self.poly:
                    time_separation = max(0, np.random.normal(
                        loc=int(self.separation),
                        scale=int(self.separation/2)
                    ))
                time.sleep(time_separation/1000)
                self.current_time += time_separation
            else:
                for i in range(127):
                    msg = Message(
                        'note_off',
                        note=i,
                        channel=self.midi_channel
                    )
                    self.outport.send(msg)
                time.sleep(0.5)
        if play_note:
            play_note.join()
        self.x_ramp.join()
