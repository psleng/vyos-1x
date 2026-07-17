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
<leafNode name="discard-chars-rxd-with-errors">
  <properties>
    <help>Enable discard characters received with errors</help>
    <valueless/>
  </properties>
</leafNode>
<node name="protocol">
  <properties>
    <help>Serial protocol (required)</help>
  </properties>
  <children>
    <node name="rs232">
      <properties>
        <help>Select RS232 protocol and configure RS232-specific options</help>
      </properties>
      <children>
        #include <include/serial/general/flow-control-all.xml.i>
        <leafNode name="monitor-signal">
          <properties>
            <help>Monitor signals</help>
            <completionHelp>
              <list>dsr dcd</list>
            </completionHelp>
            <valueHelp>
              <format>dsr</format>
              <description>Enable DTR-DSR monitor</description>
            </valueHelp>
            <valueHelp>
              <format>dcd</format>
              <description>Enable DCD monitor</description>
            </valueHelp>
            <constraint>
              <regex>(dsr|dcd)</regex>
            </constraint>
            <multi/>
          </properties>
        </leafNode>
        <node name="rts-toggle">
          <properties>
            <help>Enable RTS Toggle if your application needs for RTS to be raised during character transmission</help>
          </properties>
          <children>
            <leafNode name="final-delay">
              <properties>
                <help>Time between the time of character transmission and when RTS is dropped (in ms)</help>
                <valueHelp>
                  <format>u32:0-1000</format>
                  <description>Decimal integer (0 - 1000)</description>
                </valueHelp>
                <constraint>
                  <validator name="numeric" argument="--range 0-1000"/>
                </constraint>
              </properties>
              <defaultValue>0</defaultValue>
            </leafNode>
            <leafNode name="initial-delay">
              <properties>
                <help>Time between the time the RTS signal is raised and the start of character transmission (in ms)</help>
                <valueHelp>
                  <format>u32:0-1000</format>
                  <description>Decimal integer (0 - 1000)</description>
                </valueHelp>
                <constraint>
                  <validator name="numeric" argument="--range 0-1000"/>
                </constraint>
              </properties>
              <defaultValue>0</defaultValue>
            </leafNode>
          </children>
        </node>
      </children>
    </node>
    <node name="rs422">
      <properties>
        <help>Select RS422 protocol and configure RS422-specific options</help>
      </properties>
      <children>
        #include <include/serial/general/flow-control-none-and-software.xml.i>
        #include <include/serial/general/disable-line-termination.xml.i>
      </children>
    </node>
    <node name="rs485-full">
      <properties>
        <help>Select RS485 full duplex protocol and configure RS485 full duplex-specific options</help>
      </properties>
      <children>
        #include <include/serial/general/flow-control-none-and-software.xml.i>
        #include <include/serial/general/disable-line-termination.xml.i>
      </children>
    </node>
    <node name="rs485-half">
      <properties>
        <help>Select RS485 half duplex protocol and configure RS485 half duplex-specific options</help>
      </properties>
      <children>
        #include <include/serial/general/flow-control-none-and-software.xml.i>
        #include <include/serial/general/disable-line-termination.xml.i>
        <leafNode name="disable-echo-suppression">
          <properties>
            <help>Disable echo-suppression</help>
            <valueless/>
          </properties>
        </leafNode>
      </children>
    </node>
  </children>
</node>
<!-- include end -->
