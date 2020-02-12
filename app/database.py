# -*- coding: utf-8 -*-
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

from flask_login import UserMixin, current_user
from sqlalchemy.sql import and_
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
from app.middleware import db
from app.constants import USER_ROLES, DEFAULT_PASSWORD

mtasks = db.Table(
    'mtasks',
    db.Column('office_id', db.Integer, db.ForeignKey('offices.id'), primary_key=True),
    db.Column('task_id', db.Integer, db.ForeignKey('tasks.id'), primary_key=True))


class Mixin:
    @classmethod
    def get(cls, id):
        return cls.query.filter_by(id=id).first()


class TicketsMixin:
    def get_ticket_display_text(self):
        display_settings = Display_store.query.first()
        always_show_ticket_number = display_settings.always_show_ticket_number
        name_and_or_number = f'{getattr(self, "number", getattr(self, "ticket", "Empty"))}'

        if self.n:  # NOTE: registered ticket
            if always_show_ticket_number:
                name_and_or_number = f'{name_and_or_number} {self.name}'
            else:
                name_and_or_number = f'{self.name}'

        return name_and_or_number


class Office(db.Model, Mixin):
    __tablename__ = "offices"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.Integer, unique=True)
    timestamp = db.Column(db.DateTime(), index=True, default=datetime.utcnow)
    prefix = db.Column(db.String(2))
    operators = db.relationship('Operators', backref='operators')
    tasks = db.relationship('Task', secondary=mtasks, lazy='subquery',
                            backref=db.backref('offices', lazy=True))

    def __init__(self, name, prefix):
        self.name = name
        self.prefix = prefix

    @classmethod
    def get_first_available_prefix(cls):
        letters = list(map(lambda i: chr(i), range(97, 123)))
        prefix = None

        while letters and not prefix:
            match_letter = letters.pop()

            if not cls.query.filter_by(prefix=match_letter).first():
                prefix = match_letter

        return prefix and prefix.upper()

    @property
    def tickets(self):
        return Serial.query.filter(Serial.office_id == self.id,
                                   Serial.number != 100)


class Task(db.Model, Mixin):
    __tablename__ = "tasks"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(300))
    timestamp = db.Column(db.DateTime(), index=True, default=datetime.utcnow)

    def __init__(self, name):
        self.name = name

    @property
    def common(self):
        return len(self.offices) > 1

    @classmethod
    def get_first_common(cls):
        for task in cls.query:
            if task.common:
                return task

    def least_tickets_office(self):
        self.offices.sort(key=lambda o: Serial.query.filter_by(office_id=o.id).count())
        return self.offices[0]

    @property
    def tickets(self):
        return Serial.query.filter(Serial.task_id == self.id,
                                   Serial.number != 100)

    def migrate_tickets(self, from_office, to_office):
        params = dict(office_id=from_office.id, task_id=self.id)
        tickets = Serial.query.filter_by(**params).all()
        tickets += Waiting.query.filter_by(**params).all()

        for ticket in tickets:
            ticket.office_id = to_office.id

        db.session.commit()

class Serial(db.Model, TicketsMixin):
    __tablename__ = "serials"
    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.Integer)
    timestamp = db.Column(db.DateTime(), index=True, default=datetime.utcnow)
    date = db.Column(db.Date(), default=datetime.utcnow().date)
    name = db.Column(db.String(300), nullable=True)
    n = db.Column(db.Boolean)
    p = db.Column(db.Boolean)
    # stands for proccessed , which be modified after been processed
    pdt = db.Column(db.DateTime())
    # Fix: adding pulled by feature to tickets
    pulledBy = db.Column(db.Integer)
    on_hold = db.Column(db.Boolean, default=False)
    office_id = db.Column(db.Integer, db.ForeignKey('offices.id'))
    task_id = db.Column(db.Integer, db.ForeignKey('tasks.id'))

    def __init__(self, number=100, office_id=1, task_id=1,
    name=None, n=False, p=False, pulledBy=0):
        self.number = number
        self.office_id = office_id
        self.task_id = task_id
        self.name = name
        self.n = n
        # fixing mass use tickets multi operators conflict
        self.p = p
        self.pulledBy = pulledBy

    @property
    def task(self):
        return Task.query.filter_by(id=self.task_id).first()

    @property
    def office(self):
        return Office.query.filter_by(id=self.office_id).first()

    @property
    def puller_name(self):
        return User.get(self.pulledBy).name

    @classmethod
    def all_office_tickets(cls, office_id):
        ''' get tickets of the common task from other offices.

        Parameters
        ----------
            office_id: int
                id of the office to retreive tickets for.

        Returns
        -------
            Query of office tickets unionned with other offices tickets.
        '''
        strict_pulling = Settings.get().strict_pulling
        office = Office.get(office_id)
        all_tickets = cls.query.filter(cls.office_id == office_id,
                                       cls.number != 100)

        if not strict_pulling:
            for task in office.tasks:
                other_office_tickets = cls.query.filter(and_(cls.task_id == task.id,
                                                             cls.office_id != office_id))

                if other_office_tickets.count():
                    all_tickets = all_tickets.union(other_office_tickets)

        return all_tickets.filter(Serial.number != 100)\
                          .order_by(Serial.p, Serial.timestamp.desc())

    @classmethod
    def all_task_tickets(cls, office_id, task_id):
        ''' get tickets related to a given task and office.

        Parameters
        ----------
            office_id: int
                id of the office that we're querying from.
            task_id: int
                id of the task we want to retrieve its tickets.

        Returns
        -------
            Query of task tickets filterred based on `strict_pulling`.
        '''
        strict_pulling = Settings.get().strict_pulling
        filter_parameters = {'office_id': office_id, 'task_id': task_id}

        if not strict_pulling:
            filter_parameters.pop('office_id')

        return cls.query.filter_by(**filter_parameters)\
                        .filter(cls.number != 100)\
                        .order_by(cls.p, cls.timestamp.desc())

    def pull(self, office_id):
        ''' Mark a ticket as pulled and do the dues.

        Parameters
        ----------
            office_id: int
                id of the office from which the ticket is pulled.
        '''
        self.p = True
        self.pdt = datetime.utcnow()
        self.pulledBy = getattr(current_user, 'id', None)
        self.office_id = office_id

        db.session.add(self)
        db.session.commit()

    def toggle_on_hold(self):
        ''' Toggle the ticket `on_hold` status. '''
        self.on_hold = not self.on_hold

        db.session.add(self)
        db.session.commit()


class Waiting(db.Model, TicketsMixin):
    __tablename__ = "waitings"
    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.Integer)
    name = db.Column(db.String(300), nullable=True)
    n = db.Column(db.Boolean)
    office_id = db.Column(db.Integer, db.ForeignKey('offices.id'))
    task_id = db.Column(db.Integer, db.ForeignKey('tasks.id'))

    def __init__(self, number, office_id, task_id, name=None, n=False):
        self.number = number
        self.office_id = office_id
        self.task_id = task_id
        self.name = name
        self.n = n

    @classmethod
    def drop(cls, tickets=[]):
        ''' remove a ticket from waiting list.

        Parameters
        ----------
            tickets: list of Serial record
                ticket instance to remove from waiting list.
        '''
        for ticket in tickets:

            waiting_ticket = cls.query.filter_by(number=ticket.number)\
                                      .first()

            if waiting_ticket:
                db.session.delete(waiting_ticket)
                db.session.commit()

    @property
    def task(self):
        return Task.query.filter_by(id=self.task_id).first()

    @property
    def office(self):
        return Office.query.filter_by(id=self.office_id).first()


class Waiting_c(db.Model, TicketsMixin):
    __tablename__ = "waitings_c"
    id = db.Column(db.Integer, primary_key=True)
    ticket = db.Column(db.String)
    oname = db.Column(db.String)
    tname = db.Column(db.String)
    n = db.Column(db.Boolean)
    name = db.Column(db.String(300), nullable=True)

    def __init__(self, ticket="Empty", oname="Empty", tname="Empty",
                 n=False, name=None):
        self.id = 0
        self.ticket = ticket
        self.oname = oname
        self.tname = tname
        self.n = n
        self.name = name

    @classmethod
    def get(cls):
        cls.query.first()

    @classmethod
    def assume(cls, ticket, office, task, show_prefix):
        ''' change the current ticket details to match the passed ticket.

        Parameters
        ----------
            ticket: Serila record
                ticket to assume its properties.
            office: Office record
                office to dislay its prefix and number.
            task: Task record
                task to display its name.
            show_prefix: bool
                flag to show or hide office prefix.
        '''
        current_ticket = cls.query.first()
        prefix = office.prefix if show_prefix else ''
        current_ticket.ticket = f'{prefix}{ticket.number}'
        current_ticket.oname = f'{prefix}{office.name}'
        current_ticket.tname = task.name
        current_ticket.n = ticket.n
        current_ticket.name = ticket.name

        db.session.commit()


class Operators(db.Model, Mixin):
    __tablename__ = "operators"
    crap = db.Column(db.Integer, primary_key=True)
    id = db.Column(db.Integer)
    office_id = db.Column(db.Integer, db.ForeignKey('offices.id'))

    def __init__(self, id, office_id):
        self.id = id
        self.office_id = office_id


class User(UserMixin, db.Model, Mixin):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), unique=True)
    password_hash = db.Column(db.String(128))
    last_seen = db.Column(db.DateTime, index=True, default=datetime.utcnow)
    role_id = db.Column(db.Integer, db.ForeignKey('roles.id'))

    def __init__(self, name, password, role_id):
        self.password = password
        self.name = name
        self.role_id = role_id

    def __str__(self):
        return "<%r>" % self.name

    @property
    def password(self):
        raise AttributeError('password not for reading !!')

    @password.setter
    def password(self, password):
        self.password_hash = generate_password_hash(password)

    def verify_password(self, password):
        return check_password_hash(self.password_hash, password)

    @classmethod
    def has_default_password(cls):
        return cls.query.filter_by(id=1).first().verify_password(DEFAULT_PASSWORD)


class Roles(db.Model):
    __tablename__ = "roles"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(10), unique=True)

    def __init__(self, id, name):
        self.id = id
        self.name = name

    @classmethod
    def load_roles(cls):
        for _id, name in USER_ROLES.items():
            existing = cls.query.filter_by(id=_id, name=name)\
                                .first()

            if not existing:
                db.session.add(Roles(id=_id, name=name))
        db.session.commit()


class Printer(db.Model):
    __tablename__ = "printers"
    id = db.Column(db.Integer, primary_key=True)
    vendor = db.Column(db.String(100), nullable=True, unique=True)
    product = db.Column(db.String(100), unique=True)
    in_ep = db.Column(db.Integer, nullable=True)
    out_ep = db.Column(db.Integer, nullable=True)
    active = db.Column(db.Boolean())
    langu = db.Column(db.String(100))
    value = db.Column(db.Integer)

    def __init__(self, vendor=" ", product=" ",
                 in_ep=0, out_ep=0, active=False,
                 langu='en', value=1):
        self.vendor = vendor
        self.product = product
        self.in_ep = in_ep
        self.out_ep = out_ep
        self.active = active
        self.value = value


# 00 Configuration Tabels 00 #
# -- Touch custimization table


class Touch_store(db.Model):
    __tablename__ = 'touchs'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(300))
    message = db.Column(db.Text())
    hsize = db.Column(db.String(100))
    hcolor = db.Column(db.String(100))
    hbg = db.Column(db.String(100))
    tsize = db.Column(db.String(100))
    tcolor = db.Column(db.String(100))
    msize = db.Column(db.String(100))
    mcolor = db.Column(db.String(100))
    mbg = db.Column(db.String(100))
    audio = db.Column(db.String(100))
    hfont = db.Column(db.String(100))
    mfont = db.Column(db.String(100))
    tfont = db.Column(db.String(100))
    mduration = db.Column(db.String(100))
    bgcolor = db.Column(db.String(100))
    p = db.Column(db.Boolean)
    n = db.Column(db.Boolean)
    tmp = db.Column(db.Integer)
    akey = db.Column(db.Integer, db.ForeignKey("media.id",
                                               ondelete='cascade'),
                     nullable=True)
    ikey = db.Column(db.Integer, db.ForeignKey("media.id",
                                               ondelete='cascade'),
                     nullable=True)

    def __init__(self, id=0, title="Please select a task to pull a tick for",
                 hsize="500%", hcolor="rgb(129, 200, 139)",
                 hbg="rgba(0, 0, 0, 0.50)", tsize="400%",
                 mbg="rgba(0, 0, 0, 0.50)", msize="400%",
                 mcolor="rgb(255, 255, 0)", tcolor="btn-danger",
                 message="Ticket has been issued, pleas wait your turn",
                 audio="false", hfont="El Messiri", mfont="Mada",
                 tfont="Amiri", ikey=1, tmp=2, akey=5,
                 mduration="3000", bgcolor="rgb(0, 0, 0)", p=False, n=True):
        self.id = 0
        self.hfont = hfont
        self.mfont = mfont
        self.tfont = tfont
        self.mduration = mduration
        self.title = title
        self.message = message
        self.hsize = hsize
        self.hcolor = hcolor
        self.hbg = hbg
        self.tsize = tsize
        self.tcolor = tcolor
        self.msize = msize
        self.mcolor = mcolor
        self.mbg = mbg
        self.audio = audio
        self.bgcolor = bgcolor
        self.ikey = ikey
        self.akey = akey
        self.p = p
        self.n = n
        self.tmp = tmp


# -- Touch customization table


class Display_store(db.Model):
    __tablename__ = 'displays'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(300))
    hsize = db.Column(db.String(100))
    hcolor = db.Column(db.String(100))
    h2size = db.Column(db.String(100))
    h2color = db.Column(db.String(100))
    h2font = db.Column(db.String(100))
    hbg = db.Column(db.String(100))
    tsize = db.Column(db.String(100))
    tcolor = db.Column(db.String(100))
    ssize = db.Column(db.String(100))
    scolor = db.Column(db.String(100))
    audio = db.Column(db.String(10))
    hfont = db.Column(db.String(100))
    tfont = db.Column(db.String(100))
    sfont = db.Column(db.String(100))
    mduration = db.Column(db.String(100))
    rrate = db.Column(db.String(100))
    announce = db.Column(db.String(100))
    anr = db.Column(db.Integer)
    anrt = db.Column(db.String(100))
    effect = db.Column(db.String(100))
    repeats = db.Column(db.String(100))
    bgcolor = db.Column(db.String(100))
    tmp = db.Column(db.Integer)
    prefix = db.Column(db.Boolean, default=False)
    always_show_ticket_number = db.Column(db.Boolean, default=False)
    # adding repeat announcement value
    r_announcement = db.Column(db.Boolean)
    akey = db.Column(db.Integer, db.ForeignKey("media.id",
                                               ondelete='cascade'),
                     nullable=True)
    ikey = db.Column(db.Integer, db.ForeignKey("media.id",
                                               ondelete='cascade'),
                     nullable=True)
    vkey = db.Column(db.Integer, db.ForeignKey("media.id",
                                               ondelete='cascade'),
                     nullable=True)

    def __init__(self, id=0, title="FQM Queue Management",
                 hsize="500%", hcolor="rgb(129, 200, 139)", h2size="600%",
                 h2color="rgb(184, 193, 255)", h2font="Mada",
                 hbg="rgba(0, 0, 0, 0.5)", tsize="600%",
                 tcolor="rgb(184, 193, 255)", ssize="500%",
                 scolor="rgb(224, 224, 224)", audio="false",
                 hfont="El Messiri", tfont="Mada", repeats="3", effect="fade",
                 sfont="Amiri", mduration="3000", rrate="2000",
                 announce="en-us", ikey=4, vkey=6, akey=5,
                 anr=2, anrt="each", bgcolor="rgb(0,0,0)", tmp=1):
        self.id = 0
        self.tfont = tfont
        self.hfont = hfont
        self.h2font = h2font
        self.sfont = sfont
        self.mduration = mduration
        self.rrate = rrate
        self.title = title
        self.hsize = hsize
        self.hcolor = hcolor
        self.hsize = hsize
        self.hcolor = hcolor
        self.h2color = h2color
        self.h2size = h2size
        self.hcolor = hcolor
        self.hbg = hbg
        self.tsize = tsize
        self.tcolor = tcolor
        self.ssize = ssize
        self.scolor = scolor
        self.audio = audio
        self.announce = announce
        self.bgcolor = bgcolor
        self.tmp = tmp
        self.anr = anr
        self.anrt = anrt
        self.effect = effect
        self.repeats = repeats
        self.ikey = ikey
        self.vkey = vkey
        self.akey = akey

    @classmethod
    def get(cls):
        return cls.query.first()

# -- Slides storage table


class Slides(db.Model):
    __tablename__ = "slides"
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(300))
    hsize = db.Column(db.String(100))
    hcolor = db.Column(db.String(100))
    hfont = db.Column(db.String(100))
    hbg = db.Column(db.String(100))
    subti = db.Column(db.String(300))
    tsize = db.Column(db.String(100))
    tcolor = db.Column(db.String(100))
    tfont = db.Column(db.String(100))
    tbg = db.Column(db.String(100))
    bname = db.Column(db.String(300))
    ikey = db.Column(db.Integer, db.ForeignKey("media.id",
                                               ondelete='cascade'),
                     nullable=True)


# -- ^^ Slides custimization table

class Slides_c(db.Model):
    __tablename__ = "slides_c"
    id = db.Column(db.Integer, primary_key=True)
    rotation = db.Column(db.String(100))
    navigation = db.Column(db.Integer)
    effect = db.Column(db.String(100))
    status = db.Column(db.Integer)

    def __init__(self, rotation="3000",
                 navigation=2, effect="slide",
                 status=2):
        self.id = 0
        self.rotation = rotation
        self.effect = effect
        self.status = status
        self.navigation = navigation


class Media(db.Model):
    __tablename__ = "media"
    id = db.Column(db.Integer, primary_key=True)
    vid = db.Column(db.Boolean())
    audio = db.Column(db.Boolean())
    img = db.Column(db.Boolean())
    used = db.Column(db.Boolean())
    name = db.Column(db.String(50))

    def __init__(self, vid=False, audio=False,
                 img=False, used=False, name="   "):
        self.vid = vid
        self.audio = audio
        self.img = img
        self.used = used
        self.name = name


class Vid(db.Model):
    __tablename__ = "vids"
    id = db.Column(db.Integer, primary_key=True)
    vname = db.Column(db.String(300))
    enable = db.Column(db.Integer)
    ar = db.Column(db.Integer)
    controls = db.Column(db.Integer)
    mute = db.Column(db.Integer)
    vkey = db.Column(db.Integer, db.ForeignKey("media.id",
                                               ondelete='cascade'),
                     nullable=True)

    def __init__(self, vname="Tokyo.mp4", enable=1, ar=1, controls=1,
                 mute=2, vkey=6):
        self.vname = vname
        self.enable = enable
        self.ar = ar
        self.controls = controls
        self.mute = mute
        self.vkey = vkey


class Aliases(db.Model):
    __tablename__ = "aliases"
    id = db.Column(db.Integer, primary_key=True)
    office = db.Column(db.String(100))
    task = db.Column(db.String(100))
    ticket = db.Column(db.String(100))
    name = db.Column(db.String(100))
    number = db.Column(db.String(100))

    def __init__(self, 
        office="office", task="task", ticket="ticket",
        name="name", number="number"):
        self.id = 0
        self.office = office
        self.task = task
        self.ticket = ticket
        self.name = name
        self.number = number


class Settings(db.Model):
    __tablename__ = 'settings'
    id = db.Column(db.Integer, primary_key=True)
    notifications = db.Column(db.Boolean, nullable=True)
    strict_pulling = db.Column(db.Boolean, nullable=True)
    visual_effects = db.Column(db.Boolean, nullable=True)

    def __init__(self, notifications=True, strict_pulling=True, visual_effects=True):
        self.id = 0
        self.notifications = notifications
        self.strict_pulling = strict_pulling
        self.visual_effects = visual_effects

    @classmethod
    def get(cls):
        return cls.query.first()
