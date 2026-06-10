<!-- include start from serial/general/hardware.xml.i -->
<leafNode name="speed">
  <properties>
    <help>Baud rate</help>
    <valueHelp>
      <format>u32:300-1843200</format>
      <description>Decimal integer (300 - 1843200)</description>
    </valueHelp>
    <constraint>
      <validator name="numeric" argument="--range 300-1843200"/>
    </constraint>
  </properties>
  <defaultValue>9600</defaultValue>
</leafNode>
<leafNode name="data-bits">
  <properties>
    <help>Data bits</help>
    <completionHelp>
      <list>5 6 7 8</list>
    </completionHelp>
    <constraint>
      <regex>(5|6|7|8)</regex>
    </constraint>
  </properties>
  <defaultValue>8</defaultValue>
</leafNode>
<leafNode name="parity">
  <properties>
    <help>Parity</help>
    <completionHelp>
      <list>none odd even mark space</list>
    </completionHelp>
    <constraint>
      <regex>(none|odd|even|mark|space)</regex>
    </constraint>
  </properties>
  <defaultValue>none</defaultValue>
</leafNode>
<leafNode name="stop-bits">
  <properties>
    <help>Stop bits</help>
    <completionHelp>
      <list>1 2</list>
    </completionHelp>
    <constraint>
      <regex>(1|2)</regex>
    </constraint>
  </properties>
  <defaultValue>1</defaultValue>
</leafNode>
<leafNode name="protocol">
  <properties>
    <help>Serial protocol</help>
    <completionHelp>
      <list>rs232 rs422 rs485f rs485h</list>
    </completionHelp>
    <constraint>
      <regex>(rs232|rs422|rs485f|rs485h)</regex>
    </constraint>
  </properties>
  <defaultValue>rs232</defaultValue>
</leafNode>
<node name="rs232">
  <properties>
    <help>Config only applies to rs232</help>
  </properties>
  <children>
    <node name="flow-control">
      <properties>
        <help>Flow control</help>
      </properties>
      <children>
        <leafNode name="none">
          <properties>
            <help>No flow control</help>
            <valueless/>
          </properties>
        </leafNode>
        <node name="hardware+software">
          <properties>
            <help>Turn on hardware and software flow control</help>
          </properties>
          <children>
            #include <include/serial/general/flow-ctrl-direction.xml.i>
          </children>
        </node>
        <node name="hardware">
          <properties>
            <help>Turn on hardware flow control only</help>
          </properties>
          <children>
            #include <include/serial/general/flow-ctrl-direction.xml.i>
          </children>
        </node>
        <node name="software">
          <properties>
            <help>Turn on software flow control only</help>
          </properties>
          <children>
            #include <include/serial/general/flow-ctrl-direction.xml.i>
          </children>
        </node>
      </children>
    </node>
    <leafNode name="monitor">
      <properties>
        <help>Monitor signals</help>
        <completionHelp>
          <list>dsr dcd both</list>
        </completionHelp>
        <valueHelp>
          <format>dsr</format>
          <description>Enable DTR-DSR monitor</description>
        </valueHelp>
        <valueHelp>
          <format>dcd</format>
          <description>Enable DCD monitor</description>
        </valueHelp>
        <valueHelp>
          <format>both</format>
          <description>Enable both DTR-DSR and DCD monitors</description>
        </valueHelp>
        <constraint>
          <regex>(dsr|dcd|both)</regex>
        </constraint>
      </properties>
    </leafNode>
    <node name="rts-toggle">
      <properties>
        <help>Enable RTS Toggle if your application needs for RTS to be raised during character transmission</help>
      </properties>
      <children>
        <leafNode name="final-delay">
          <properties>
            <help>Time between the time of character transmission and when RTS is dropped (in ms, default: 0)</help>
            <valueHelp>
              <format>u32:0-1000</format>
              <description>Decimal integer (0 - 1000)</description>
            </valueHelp>
            <constraint>
              <validator name="numeric" argument="--range 0-1000"/>
            </constraint>
          </properties>
        </leafNode>
        <leafNode name="initial-delay">
          <properties>
            <help>Time between the time the RTS signal is raised and the start of character transmission (in ms, default: 0)</help>
            <valueHelp>
              <format>u32:0-1000</format>
              <description>Decimal integer (0 - 1000)</description>
            </valueHelp>
            <constraint>
              <validator name="numeric" argument="--range 0-1000"/>
            </constraint>
          </properties>
        </leafNode>
      </children>
    </node>
  </children>
</node>
<node name="rs422-485">
  <properties>
    <help>Config only applies to rs422 and rs485</help>
  </properties>
  <children>
    <leafNode name="disable-line-termination">
      <properties>
        <help>Disable line-termination for rs422 and rs485</help>
        <valueless/>
      </properties>
    </leafNode>
    <node name="flow-control">
      <properties>
        <help>Flow control</help>
      </properties>
      <children>
        <leafNode name="none">
          <properties>
            <help>No flow control</help>
            <valueless/>
          </properties>
        </leafNode>
        <node name="software">
          <properties>
            <help>Turn on software flow control only</help>
          </properties>
          <children>
            #include <include/serial/general/flow-ctrl-direction.xml.i>
          </children>
        </node>
      </children>
    </node>
  </children>
</node>
<node name="rs485h">
  <properties>
    <help>Config only applies to rs485 half duplex</help>
  </properties>
  <children>
    <leafNode name="disable-echo-suppression">
      <properties>
        <help>Disable echo-suppression for rs485 half duplex only</help>
        <valueless/>
      </properties>
    </leafNode>
    <leafNode name="tx-driver-control">
      <properties>
        <help>Transmit driver control for rs485 half duplex only</help>
        <completionHelp>
          <list>auto rts</list>
        </completionHelp>
        <constraint>
          <regex>(auto|rts)</regex>
        </constraint>
      </properties>
      <defaultValue>auto</defaultValue>
    </leafNode>
  </children>
</node>
<!-- include end -->
