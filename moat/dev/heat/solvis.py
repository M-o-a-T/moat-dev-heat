#!/usr/bin/python3

# This program controls a SolvisMax+SolvisLea combination "manually"
# because the SolvisMax controller doesn't support a heap of features I need.
# 
# See the accompanying .rst file for details.
#
import sys
sys.path.insert(0,".")

import io
import time
import anyio
import RPi.GPIO as GPIO
import asyncclick as click
import logging
from enum import IntEnum,auto

from moat.lib.pid import CPID

from moat.util import yload,yprint,attrdict, PathLongener, P,to_attrdict,to_attrdict, pos2val,val2pos
from moat.kv.client import open_client

FORMAT = (
    "%(levelname)s %(pathname)-15s %(lineno)-4s %(message)s"
)
logging.basicConfig(level=logging.INFO,format=FORMAT)

GPIO.setmode(GPIO.BCM)
logger = logging.root

class Run(IntEnum):
    # nothing happens
    off=0
    # wait for pump to be ready after doing whatever
    wait_time=auto()
    # wait for throughput to show up
    wait_flow=auto()
    # wait for decent throughput
    flow=auto()
    # wait for pump to draw power
    wait_power=auto()
    # wait for outflow-inflow>2
    temp=auto()
    # operation
    run=auto()
    # ice
    ice=auto()
    # wait for outflow-inflow<2 for n seconds, cool down
    down=auto()

CFG="""
state: "/tmp/solvis.state"
adj:
    # offsets to destination temperature
    water: 3
    heat: 1.5
    more: 2  # output temperature offset
    max: 61  # don't try for more
    low:
        water: 1
        heat: .5
        buffer: -2
output:
    flow:
        pin: 4
        freq: 200
        path: !P heat.s.pump.pid
sensor:
    pump:
        in: !P heat.s.pump.temp.in   # t_in
        out: !P heat.s.pump.temp.out # t_out
        flow: !P heat.s.pump.flow    # r_flow: flow rate
        ice: !P heat.s.pump.de_ice   # m_ice
    buffer:
        top: !P heat.s.buffer.temp.water   # tb_water
        heat: !P heat.s.buffer.temp.heat   # tb_heat
        mid: !P heat.s.buffer.temp.mid     # tb_mid
        low: !P heat.s.buffer.temp.return  # tb_low
    error: !P heat.s.pump.err
    power: !P heat.s.pump.power  # m_power
    cop: !P home.ass.dyn.sensor.heizung.wp_cop.state  # m_cop
setting:
    heat:
      day: !P heat.s.heat.temp        # c_heat
      night: !P heat.s.heat.temp_low  # c_heat_night
      power: 19
      mode:
        path: !P heat.s.heat.mode.cmd    # 5:standby 2:auto 3:day 4:night
        on: 3
        off: 5
        delay: 30
    water: !P heat.s.water.temp         # c_water
    passthru: !P heat.s.pump.pass       # m_passthru
lim:
    flow:
        min: 5
    power:
        min: .04
        off: .2   # power cutoff if the buffer is warm enough
        time: 300  # must run at least this long
cmd:
    flow: !P heat.s.pump.rate.cmd       # c_flow
    main: !P home.ass.dyn.switch.heizung.wp.cmd  # cm_main
    heat: !P home.ass.dyn.switch.heizung.main.cmd  # cm_heat
    mode:
      path: !P heat.s.pump.cmd.mode       # write
      on: 3
      off: 0
    power: !P heat.s.pump.cmd.power
feedback:
    main: !P home.ass.dyn.switch.heizung.wp.state
    heat: !P home.ass.dyn.switch.heizung.main.state
    ice: !P home.ass.dyn.binary_densor.heizung.wp_de_ice.state
misc:
    init_timeout: 5
    de_ice: 17  # flow rate when de-icing
    stop:
      flow: 10  # or more if the max outflow temperature wants us to
      delta: 3  # outflow-inflow: if less than .delta, the pump can be turned off
    start: # conditions when starting up
      delay: 330  # 10min
      flow:
        init:
          rate: 6
          pwm: .25
        power:
          rate: 15
          pwm: .4
        run: 15
      power: 0.1
      delta: 2  # outflow-inflow: if more than .delta, we start the main control algorithm
      # TODO add pump power uptake, to make sure this is no fluke
    min_power: 0.9
pid:
    flow:
        ## direct flow rate control for the pump
        # input: desired flow rate
        # output: PWM for the flow pump
        p: 0.02   # half of 1/20
        i: 0.0003
        d: 0.0
        tf: 0.0

        min: .25
        max: .95

        # setpoint change
        # .8 == 20 l/min
        factor: 0 # .04
        offset: 0

        # state attr
        state: p_flow

    pump:
        ## indirect flow rate control. The heat pump delivers some amount of energy;
        ## we want the flow rate to be such that the temperature of the outflow is
        ## what we want. Too high and the efficiency suffery; too low and we don't get
        ## the temperature we want.
        #
        # setpoint: desired buffer temperature, plus offset
        # input: exchanger output temperature
        # output: PWM for the flow pump
        ## Adjust the flow to keep the output temperature within range.
        p: -0.05
        i: -0.001
        d: 0.0
        tf: 0.0

        min: .2
        max: 1

        factor: 0
        offset: 0

        # state attr
        state: p_pump

    load:
        ## Primary heat exchanger control. We want the top buffer temperature to be at a certain value.
        ## 
        # setpoint: desired buffer temperature
        # input: buffer temperature
        # output: heat exchanger load
        ## Add as much load as required to keep the buffer temperature up.
        p: 0.08
        i: 0.0005
        d: 0.0
        tf: 0.0

        min: .04
        max: 1

        # no setpoint change, always zero
        # 
        # 50: 5 /min
        # 
        factor: 0
        offset: 0

        # state attr
        state: p_load

    buffer:
        ## More heat exchanger control. We want the bottom buffer temperature not to get too high.
        ## 
        # setpoint: desired buffer temperature
        # input: buffer temperature
        # output: heat exchanger load
        ## Reduce the load as required to keep the buffer temperature down.
        p: 0.2
        i: 0.0001
        d: 0.0
        tf: 0.0

        min: .04
        max: 1

        # no setpoint change, always zero
        # 
        # 50: 5 /min
        # 
        factor: 0
        offset: 0

        # state attr
        state: p_buffer

    limit:
        ## Secondary heat exchanger control. We don't want more load than the pump can transfer
        ## to the buffer, otherwise the system overheats.
        #
        # setpoint: desired buffer temperature plus adj.more
        # input: exchanger output temperature
        # output: heat exchanger load
        p: 0.05
        i: 0.0007
        d: 0.0
        tf: 0.0

        min: .04
        max: 1

        # setpoint change
        # 65: 20 /min
        # 50: 5 /min
        # 
        factor: 0 # 1
        offset: 0 # -45

        # state attr
        state: p_limit

"""


with open("/etc/moat/moat.cfg","r") as _f:
    mcfg = yload(_f, attr=True)

class Data:
    force_on=False

    def __init__(self, cfg, cl, record=None):
        self._cfg = cfg
        self._cl = cl
        self._got = anyio.Event()
        self._want = set()
        self._sigs = {}
        self.record=record

        try:
            with open(cfg.state,"r") as sf:
                self.state = yload(sf, attr=True)
        except EnvironmentError:
            self.state = attrdict()

        # calculated pump flow rate, 0…1
        self.cp_flow = None
        self.m_errors = {}

        self.pid_load = CPID(self.cfg.pid.load, self.state)
        self.pid_buffer = CPID(self.cfg.pid.buffer, self.state)
        self.pid_limit = CPID(self.cfg.pid.limit, self.state)
        self.pid_pump = CPID(self.cfg.pid.pump, self.state)
        self.pid_flow = CPID(self.cfg.pid.flow, self.state)
        self.state.setdefault("heat_ok",False)

        try:
            path = self.cfg.output.flow.path
        except AttributeError:
            pin = self.cfg.output.flow.pin
            GPIO.setup(pin, GPIO.OUT)
            self._flow_port = port = GPIO.PWM(pin, 200)
            port.start(0)
            async def set_flow_pwm(r):
                self.state.last_pwm = r
                port.ChangeDutyCycle(100*r)
        else:
            async def set_flow_pwm(r):
                self.state.last_pwm = r
                await self.cl.set(path,value=r, idem=True)
        self.set_flow_pwm = set_flow_pwm

    @property
    def time(self):
        try:
            return self.TS
        except AttributeError:
            return time.monotonic()

    @property
    def cl(self):
        return self._cl

    @property
    def cfg(self):
        return self._cfg

    # async def set_flow_pwm(self, rate):
    # added by .run_flow

    async def set_load(self, p):
        if p < self.cfg.lim.power.min:
            await self.cl.set(self.cfg.cmd.power, value=0, idem=True)
            await self.cl.set(self.cfg.cmd.mode.path, value=self.cfg.cmd.mode.off, idem=True)
            self.state.last_load = 0
        else:
            await self.cl.set(self.cfg.cmd.power, value=min(p,1), idem=True)
            await self.cl.set(self.cfg.cmd.mode.path, value=self.cfg.cmd.mode.on, idem=True)
            self.state.last_load = p

    async def run_pump(self, *, task_status=anyio.TASK_STATUS_IGNORED):
        cm_main = None
        run = Run(self.state.get("run",0))
        # 0 off; 1 go_up, 2 up, 3 go_down
        # TODO charge the hot water part separately
        tlast=0

        orun = None
        m_ice = False
        flow_on = None
        cm_main = None
        cm_heat = None
        n_cop = 0
        t_no_power = None
        heat_off=False
        water_ok = True
        heat_pin = self.cfg.setting.heat.get("power", None)
        if heat_pin is not None:
            GPIO.setup(heat_pin, GPIO.OUT)

        while True:
            #
            # Part 1: what to do when a state changes
            #
            # check for inappropriate state changes
            #
            if orun == run:
                pass
            elif orun is None:
                pass
            elif run == Run.off:
                pass
            elif run == Run.ice:
                pass
            elif run.value == orun.value+1:
                pass
            elif orun != Run.off and run == Run.down:
                pass
            elif orun == Run.down and run == Run.off:
                pass
            else:
                raise ValueError(f"Cannot go from {orun.name} to {run.name}")

            # Handle state changes

            # redirect for shutdown
            if run == Run.off:
                if orun not in (Run.off,Run.wait_time,Run.wait_flow,Run.wait_power,Run.down):
                    run = Run.down

            # Report
            if orun != run:
                print(f"*** STATE: {run.name}")

            # Leaving a state
            if orun is None:  # fix stuff for startup
                # assume PIDs are restored from state
                # assume PWM is stable
                if run == Run.run:
                    last = self.state.get("load_last",None)
                    if last is not None:
                        await self.set_load(last)

            elif orun == run:  # no change
                pass

            elif orun == Run.down:
                pass

            oheat_off,heat_off = heat_off,None

            # Entering a state
            if orun == run:  # no change
                heat_off = oheat_off

                task_status.started()
                task_status = anyio.TASK_STATUS_IGNORED
                await self.wait()

            elif run == Run.off:  # nothing happens
                heat_off=False
                await self.set_flow_pwm(0)
                await self.set_load(0)
                self.state.last_pwm = None

            elif run == Run.wait_time:  # wait for the heat pump to be ready after doing whatever
                await self.set_flow_pwm(0)
                await self.set_load(0)

            elif run == Run.wait_flow:  # wait for flow
                heat_off=True
                await self.cl.set(self.cfg.cmd.mode.path, value=self.cfg.cmd.mode.off)
                await self.set_flow_pwm(self.cfg.misc.start.flow.init.pwm)

            elif run == Run.flow:  # wait for decent throughput
                pass

            elif run == Run.wait_power:  # wait for pump to draw power
                self.pid_flow.move_to(self.r_flow,self.state.last_pwm)
                await self.set_load(self.cfg.misc.start.power)

            elif run == Run.temp:  # wait for outflow-inflow>2
                self.pid_flow.setpoint(self.cfg.misc.start.flow.power.rate)
                await self.set_flow_pwm(self.cfg.misc.start.flow.power.pwm)

            elif run == Run.run:  # operation
                heat_off=False
                self.state.setdefault("t_run", self.time)
                if orun is not None:
                    self.pid_limit.reset()
                    self.pid_load.reset()
                    self.pid_buffer.reset()
                self.pid_pump.move_to(self.t_out, self.state.last_pwm, t=self.time)
                self.state.load_last = None

            elif run == Run.ice:  # wait for ice condition to stop
                heat_off=True
                self.pid_flow.setpoint(self.cfg.misc.de_ice)
                await self.cl.set(self.cfg.cmd.mode.path, value=self.cfg.cmd.mode.off)
                await self.cl.set(self.cfg.cmd.power, value=0)

            elif run == Run.down:  # wait for outflow-inflow<2 for n seconds, cool down
                heat_off=True
                self.pid_flow.setpoint(self.cfg.misc.stop.flow)
                await self.cl.set(self.cfg.cmd.mode.path, value=self.cfg.cmd.mode.off)
                await self.cl.set(self.cfg.cmd.power, value=0)

            else:
                raise ValueError(f"State ?? {run !r}")

            orun = run
            self.state.run = int(run)

            # When de-icing starts, shut down (for now).
            if self.m_ice:
                if not m_ice:
                    print("*** ICE ***")
                    m_ice = True
                    await self.cl.set(self.cfg.feedback.ice, True)
                    run = Run.ice
                    continue
            else:
                if m_ice:
                    await self.cl.set(self.cfg.feedback.ice, False)
                    print("*** NO ICE ***")
                m_ice = False

            # Process the main switch

            if not self.cm_main or bool(self.m_errors):
                if cm_main:
                    cm_main = False
                    await self.cl.set(self.cfg.feedback.main, self.cm_main)
                    run = Run.off
                    continue
            else:
                if not cm_main:
                    cm_main = True
                    await self.cl.set(self.cfg.feedback.main, self.cm_main)

            # Process the heating control switch

            if self.cm_heat != cm_heat:
                await self.cl.set(self.cfg.feedback.heat, self.cm_heat)
                cm_heat = self.cm_heat

            # Calculate desired temperatures

            # TODO:

            # we need three states:
            # - water only
            # - water while (water OR heating) is too low  # TODO, needs switch
            # - heating while water is sufficient
            # 
            # buffer temp < nominal: pump speed: deliver nominal+adj_low
            # buffer temp > nominal+adj: pump speed: deliver MAXTEMP
            # in between: interpolate
        
            # The system should be able to run in either steady-state or max-charge mode.

            tw_nom = self.c_water
            tw_low = tw_nom + self.cfg.adj.low.water
            tw_adj = tw_nom + self.cfg.adj.water

            th_nom = self.c_heat
            th_low = th_nom + self.cfg.adj.low.heat
            th_adj = th_nom + self.cfg.adj.heat

            ## TODO add an output to switch the supply
            if True:
                pass
            elif tb_water < tw_low:
                water_ok = False
            elif tb_water >= tw_adj and tb_heat >= tw_low:
                water_ok = True

            if cm_heat and water_ok:
                t_nom = max(th_nom, tw_nom)
                t_low = max(th_low, tw_low)
                t_adj = max(th_adj, tw_adj)

                t_cur = self.tb_heat
            else:
                t_nom = tw_nom
                t_low = tw_low
                t_adj = tw_adj

                t_cur = self.tb_water

            # PID controller settings
            f = val2pos(t_nom,t_cur,t_adj, clamp=True)
            t_limit = min(self.cfg.adj.max, t_adj+self.cfg.adj.more)
            t_pump = pos2val(t_low,f,t_limit)
            t_load = t_adj+self.cfg.adj.more
            t_buffer = t_low+self.cfg.adj.low.buffer

            # on/off thresholds
            t_set_on = (t_low+t_adj)/2  # top
            t_set_off = t_nom




            # if tt-tlast>5 or self.t_out>self.cfg.adj.max:
            if cm_heat:
                # * 
                # less than this much doesn't make sense
                th_min = self.c_heat+self.cfg.adj.low.heat
                # more than this doesn't either
                th_max = self.c_heat+self.cfg.adj.heat

                # the boundary is the adjusted current buffer temperature, except when the load is low
                t_set = min(th_max,max(th_min, self.tb_heat-self.cfg.adj.low.heat))



            # State handling

            if run == Run.off:  # nothing happens
                if not cm_main:
                    r="main"
                elif bool(self.m_errors):
                    r="errn"
                elif self.force_on or t_cur < t_set_on:
                    # TODO configureable threshold?
                    run = Run.wait_time
                    self.force_on=False
                    continue
                else:
                    r="temp"
                print(f"      -{r} cur={t_cur :.1f} on={t_set_on :.1f}",end="\r"); sys.stdout.flush()

            elif run == Run.wait_time:  # wait for pump to be ready after doing whatever
                if self.state.get("t_load",0) + self.cfg.misc.start.delay < self.time:
                    run = Run.wait_flow
                    continue

            elif run == Run.wait_flow:  # wait for decent throughput
                if self.r_flow:
                    run = Run.flow
                    continue

            elif run == Run.flow:  # wait for decent throughput
                if self.r_flow >= self.cfg.misc.start.flow.init.rate*3/4:
                    run = Run.wait_power
                    continue
                l_flow = self.pid_flow(self.r_flow, t=self.time)
                print(f"Flow: {self.r_flow :.1f} : {l_flow :.3f}")
                await self.set_flow_pwm(l_flow)

            elif run == Run.wait_power:  # wait for pump to draw power
                if self.m_power >= self.cfg.misc.min_power:
                    run = Run.temp
                    continue
                l_flow = self.pid_flow(self.r_flow, t=self.time)
                print(f"Flow: {self.r_flow :.1f} : {l_flow :.3f} p={self.m_power :.1f}")
                await self.set_flow_pwm(l_flow)

            elif run == Run.temp:  # wait for outflow-inflow>2
                self.state.t_load = self.time
                if self.t_out-self.t_in > self.cfg.misc.start.delta:
                    run = Run.run
                    continue
                await self.handle_flow()

            elif run == Run.run:  # operation
                # see below for the main loop
                self.state.t_load = self.time

            elif run == Run.ice:  # no operation
                await self.handle_flow()
                if not self.m_ice:
                    run = Run.down
                    continue

            elif run == Run.down:  # wait for outflow-inflow<2 for n seconds, cool down
                await self.handle_flow()
                if self.t_out-self.t_in < self.cfg.misc.stop.delta:
                    run = Run.off
                    continue

            else:
                raise ValueError(f"State ?? {run !r}")


            heat_ok = (not heat_off) and min(self.tb_heat,self.t_out if self.state.last_pwm else 9999) >= self.c_heat
            if not heat_ok:
                # If the incoming water is too cold, turn off heating
                # We DO NOT turn heating off while running: danger of overloading the heat pump
                # due to temperature jump, because the backflow is now fed by warm buffer
                # from the buffer instead of cold water returning from radiators,
                # esp. when they have been cooling off for some time
                if run != Run.run and self.state.heat_ok is not False:
                    print("HC 1",end="\r");sys.stdout.flush();
                    if heat_pin is None:
                        await self.cl.set(self.cfg.setting.heat.mode.path, self.cfg.setting.heat.mode.off)
                    else:
                        GPIO.output(heat_pin,False)
                    self.state.heat_ok = False
                else:
                    print("HC 2",end="\r");sys.stdout.flush();
            elif self.state.heat_ok is True:
                print("HC 3",end="\r");sys.stdout.flush();
                pass
            elif self.state.heat_ok is False:
                print("HC 4",end="\r");sys.stdout.flush();
                self.state.heat_ok = self.time
            elif self.time-self.state.heat_ok > self.cfg.setting.heat.mode.delay:
                print("HC 5",end="\r");sys.stdout.flush();
                if heat_pin is None:
                    await self.cl.set(self.cfg.setting.heat.mode.path, self.cfg.setting.heat.mode.on)
                else:
                    GPIO.output(heat_pin,True)
                self.state.heat_ok = True
            else:
                # wait
                print("HC 6",end="\r");sys.stdout.flush();
                pass

            if run != Run.run:
                continue
                # END not running

            # RUNNING ONLY after this point


            if self.m_power < self.cfg.misc.min_power:
                # might be ice or whatever, so wait
                if t_no_power is None:
                    t_no_power = self.time
                elif self.time-t_no_power > 20:
                    print(" NO POWER USE")
                    run = Run.off
                    continue
            else:
                t_no_power = None

            if self.pid_load.state.get("setpoint",None) != t_load:
                logger.info("Load SET %.3f",t_load)
                self.pid_load.setpoint(t_load)

            if self.pid_buffer.state.get("setpoint",None) != t_buffer:
                logger.info("Buffer SET %.3f",t_buffer)
                self.pid_buffer.setpoint(t_buffer)

            if self.pid_limit.state.get("setpoint",None) != t_limit:
                logger.info("Limit SET %.3f",t_limit)
                self.pid_limit.setpoint(t_limit)

            if self.pid_pump.state.get("setpoint",None) != t_pump:
                logger.info("Pump SET %.3f",t_pump)
                self.pid_pump.setpoint(t_pump)
            
            # The pump rate is controlled by its intended output heat now
            if self.t_out>self.cfg.adj.max:
                l_pump=1
                # emergency handler
            else:
                l_pump = self.pid_pump(self.t_out, self.time)
                self.pid_flow.move_to(self.r_flow, l_pump, t=self.time)

            l_load = self.pid_load(t_cur, t=self.time)
            l_buffer = self.pid_buffer(self.tb_low, t=self.time)
            l_limit = self.pid_limit(self.t_out, t=self.time)
            lim=min(l_load,l_buffer,l_limit)

            tt = self.time
            if tt-tlast>5 or self.t_out>self.cfg.adj.max:
                tlast=tt
                pr = (
                        f"t={int(tt)%1000:03d}",
                        f"buf={t_cur :.1f}/{self.tb_mid :.1f}/{self.tb_low :.1f}",
                        f"t={self.t_out :.1f}/{self.t_in :.1f}",
                        f"Pump={l_pump :.3f}",
                        f"load{'=' if lim == l_load else '_'}{l_load :.3f}",
                        f"buf{'=' if lim == l_buffer else '_'}{l_buffer :.3f}",
                        f"lim{'=' if lim == l_limit else '_'}{l_limit :.3f}",
                      )
                print(*pr)
            await self.set_load(lim)
            await self.set_flow_pwm(l_pump)
            self.state.load_last = lim


            # COP
            if self.m_power:
                cop = 1.16*60*self.r_flow*(self.t_out-self.t_in)/1000/self.m_power
                self.m_cop += 0.001*(cop-self.m_cop)
                if n_cop <= 0:
                    n_cop = 100
                    await self.cl.set(self.cfg.sensor.cop, self.m_cop)
                else:
                    n_cop -= 1


            # Finally, we might want to turn the heat exchanger off.

            # Buffer head temperature high enough?
            if t_cur >= t_adj:

                # Running long enough or temperature *really* high?
                if self.time-self.state.t_run > self.cfg.lim.power.time and self.tb_mid >= t_set_off:
                    run=Run.off
                    continue
                elif self.tb_low >= t_low:
                    run=Run.off
                    continue


    async def handle_flow(self):
        """
        Flow handler while not operational
        """
        l_flow = self.pid_flow(self.r_flow, t=self.time)
        l_temp = self.pid_pump(self.t_out, t=self.time)
        print(f"t={self.time%1000 :03.0f} Pump:{l_flow :.3f}/{l_temp :.3f} flow={self.r_flow :.1f} t={self.t_out :.1f}")
        res = max(l_flow,l_temp)
        # self.pid_flow.move_to(self.r_flow, res, t=self.time)
        # self.pid_pump.move_to(self.t_out, res, t=self.time)
        await self.set_flow_pwm(res)
        self.state.last_pump = res


    def has(self, name, value):
        setattr(self,name,value)
        if (evt := self._sigs.get(name)) is not None:
            evt.set()
        self.trigger()

    def trigger(self):
        if self.record:
            d=attrdict((k,v) for k,v in vars(self).items() if not k.startswith("_") and isinstance(v,(int,float,dict,tuple,list)))
            d.TS=self.time
            yprint(d, self.record)
            print("---",file=self.record)
        self._got.set()
        self._got = anyio.Event()

    async def err_mon(self, *, task_status=anyio.TASK_STATUS_IGNORED):
        async with self.cl.watch(self.cfg.sensor.error, long_path=False,fetch=True) as msgs:
            task_status.started()
            errs = self.m_errors
            err_base = P("pump")
            pl = PathLongener(())
            async for m in msgs:
                if "value" not in m:
                    continue
                pl(m)
                err = err_base+m.path
                was = bool(errs)
                if m.value:
                    if m.value == 30055:
                        print("WP COMM ERR")
                    elif m.path == P(":1"):
                        print("ERROR",m.value)
                    else:
                        print("ERROR",m.path,m.value)
                        errs[err] = m.value
                else:
                    errs.pop(err,None)
                if was or errs:
                    print("****** ERROR ********",errs)
                self.trigger()


    async def wait(self):
        await self._got.wait()

    async def wait_for(self, v):
        if (evt := self._sigs.get(v)) is not None:
            await evt.wait()
        else:
            self._sigs[v] = evt = anyio.Event()
            await evt.wait()
            del self._sigs[v]

    async def all_done(self):
        """
        Wait for startup to be completed.

        Complain once per second about missing values, assuming there's a change.
        """
        while self._want:
            t = self.time
            print("Waiting",self._want)
            await self._got.wait()
            while (t2 := self.time)-t < 1:
                if not self._want:
                    break
                with anyio.move_on_after(t2-t):
                    await self._got.wait()

    async def _kv(self,p,v,*,task_status=anyio.TASK_STATUS_IGNORED):
        self._want.add(v)
        miss = False
        task_status.started()
        async with self._cl.watch(p,max_depth=0,fetch=True) as msgs:
            async for m in msgs:
                if m.get("state","") == "uptodate":
                    if hasattr(self,v):
                        miss = False
                        self._want.remove(v)
                        if not self._want:
                            self.trigger()

                    else:
                        logger.warning("Missing: %r:%r", p,v)
                        miss = True
                elif "value" not in m:
                    logger.warning("Unknown: %r:%r: %r", p,v,m)
                else:
                    logger.debug("Value: %r:%r", p,m.value)
                    if miss:
                        miss = False
                        self._want.remove(v)
                    self.has(v,m.value)

    async def off(self, *, task_status=anyio.TASK_STATUS_IGNORED):
        async with anyio.create_task_group() as tg:
            run = Run(self.state.get("run",0))
            self.state.run = Run.down.value
            await self.save()

            await self.set_load(0)
            task_status.started()
            if self.t_out-self.t_in > self.cfg.misc.stop.delta:
                while self.t_out-self.t_in > self.cfg.misc.stop.delta:
                    await self.handle_flow()
                    await self.wait()

            tg.cancel_scope.cancel()

        await self.set_flow_pwm(0)
        self.state.run = 0
        await self.save()

    async def run(self, *, task_status=anyio.TASK_STATUS_IGNORED):
        try:
            await self.run_pump(task_status=task_status)
        except BaseException as exc:
            e = exc
        else:
            e = None
        finally:
            print(f"*** OFF {e !r} ***")
            with anyio.CancelScope(shield=True):
                await self.off()

    async def run_init(self, *, task_status=anyio.TASK_STATUS_IGNORED):
        cfg = self._cfg
        async with anyio.create_task_group() as tg:
            await tg.start(self._kv, cfg.cmd.flow, "c_flow")
            await tg.start(self._kv, cfg.cmd.main, "cm_main")
            await tg.start(self._kv, cfg.cmd.heat, "cm_heat")
            await tg.start(self._kv, cfg.setting.heat.day, "c_heat")
            await tg.start(self._kv, cfg.setting.heat.night, "c_heat_night")
            await tg.start(self._kv, cfg.setting.water, "c_water")
            await tg.start(self._kv, cfg.setting.passthru, "m_passthru")
            await tg.start(self._kv, cfg.sensor.pump["in"], "t_in")
            await tg.start(self._kv, cfg.sensor.pump["out"], "t_out")
            await tg.start(self._kv, cfg.sensor.pump.flow, "r_flow")
            await tg.start(self._kv, cfg.sensor.pump.ice, "m_ice")
            await tg.start(self._kv, cfg.sensor.cop, "m_cop")
            await tg.start(self._kv, cfg.sensor.buffer.top, "tb_water")
            await tg.start(self._kv, cfg.sensor.buffer.heat, "tb_heat")
            await tg.start(self._kv, cfg.sensor.buffer.mid, "tb_mid")
            await tg.start(self._kv, cfg.sensor.buffer.low, "tb_low")
            await tg.start(self._kv, cfg.sensor.power, "m_power")

            try:
                with anyio.fail_after(self.cfg.misc.init_timeout):
                    await self.all_done()
            except TimeoutError:
                raise ValueError("missing:"+repr(self._want)) from None
            task_status.started()
            yprint({k:v for k,v in vars(self).items() if not k.startswith("_") and isinstance(v,(int,float,str))})


    async def run_rec(self, rec, tg, *, task_status=anyio.TASK_STATUS_IGNORED):
        task_status.started()
        t = None
        for r in yload(rec, multi=True):
            if r is None:
                print("END RECORDING")
                for _ in range(100):
                    await anyio.sleep(0.001)
                tg.cancel_scope.cancel()
                return
            self.__dict__.update(r)
            self.state = to_attrdict(self.state)
            self.trigger()
            for _ in range(20):
                await anyio.sleep(0.001)


    async def run_fake(self, *, task_status=anyio.TASK_STATUS_IGNORED):
        async def fkv(var):
            while not hasattr(self,var):
                await self.wait()
        async with anyio.create_task_group() as tg:
            await fkv("c_flow")
            await fkv("cm_main")
            await fkv("cm_heat")
            await fkv("c_heat")
            await fkv("c_heat_night")
            await fkv("c_water")
            await fkv("m_passthru")
            await fkv("t_in")
            await fkv("t_out")
            await fkv("r_flow")
            await fkv("m_ice")
            await fkv("tb_water")
            await fkv("tb_heat")
            await fkv("tb_mid")
            await fkv("tb_low")
            await fkv("m_power")
            task_status.started()

    async def saver(self, *, task_status=anyio.TASK_STATUS_IGNORED):
        task_status.started()
        while True:
            await anyio.sleep(10)
            await self.save()
            await self.wait()

    async def save(self):
        logger.debug("Saving")
        f = anyio.Path(self.cfg.state)
        fn = anyio.Path(self.cfg.state+".n")
        fs = io.StringIO()
        yprint(self.state,fs)
        await fn.write_text(fs.getvalue())
        await fn.rename(f)

class fake_cl:
    def __init__(self):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self, *tb):
        pass
    async def set(self,path,value,**k):
        print("SET",path,value)

#GPIO.setup(12, GPIO.OUT)
# 
#p = GPIO.PWM(12, .2)  # frequency=50Hz
#p.start(50)
#try:
#    while 1:
#        time.sleep(10)
#except KeyboardInterrupt:
#    p.stop()
#    GPIO.cleanup()

@click.group
@click.pass_context
@click.option("-c","--config",type=click.File("r"), help="config file")
async def cli(ctx, config):
    ctx.obj = attrdict()
    if config is not None:
        cfg = yload(config,attr=True)
    else:
        cfg = yload(CFG,attr=True)
    ctx.obj.cfg = cfg
    pass

@cli.command
@click.pass_obj
@click.option("-r","--record",type=click.File("w"))
@click.option("-f","--force-on",is_flag=True)
async def run(obj,record,force_on):
    async with open_client(**mcfg.kv) as cl, anyio.create_task_group() as tg:
        d = Data(obj.cfg,cl, record=record)
        d.force_on=force_on

        await tg.start(d.run_init)
        await tg.start(d.err_mon)
        await tg.start(d.run)
        await tg.start(d.saver)


@cli.command
@click.pass_obj
@click.argument("record",type=click.File("r"))
async def replay(obj,record):
    async with fake_cl() as cl, anyio.create_task_group() as tg:
        d = Data(obj.cfg,cl)
        await tg.start(d.run_rec, record, tg)
        await tg.start(d.run_fake)
        await tg.start(d.run)


@cli.command
@click.pass_obj
async def pwm(obj):
    """
    Run backgrounds task for software PWM outputs.
    """
    async with open_client(**mcfg.kv) as cl, anyio.create_task_group() as tg:
        for k,p in obj.cfg.output.items():
            tg.start_soon(_run_pwm,cl,k,p)

async def _run_pwm(cl,k,v):
    GPIO.setup(v.pin, GPIO.OUT)
    port = GPIO.PWM(v.pin, v.get("freq",200))
    port.start(0)
    async with cl.watch(v.path,max_depth=0,fetch=True) as msgs:
        async for m in msgs:
            if m.get("state","") == "uptodate":
                pass
            elif "value" not in m:
                logger.warning("Unknown: %s:%r: %r", k,v,m)
            else:
                logger.info("Value: %s:%r", k,m.value)
                port.ChangeDutyCycle(100*m.value)

@cli.command
@click.pass_obj
async def off(obj):
    async with open_client(**mcfg.kv) as cl, anyio.create_task_group() as tg:
        d = Data(obj.cfg,cl)
        await tg.start(d.run_init)
        await d.off()
        tg.cancel_scope.cancel()