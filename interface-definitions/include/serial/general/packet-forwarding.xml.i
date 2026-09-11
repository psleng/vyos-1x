<!-- include start from serial/general/packet-forwarding.xml.i -->
<node name="packet-forwarding">
  <properties>
    <help>Packet forwarding setting</help>
  </properties>
  <children>
    <leafNode name="minimize-latency">
      <properties>
        <help>All application data is immediately forwarded to the serial device and that every character received from the serial device is immediately sent on the network (default)</help>
        <valueless/>
      </properties>
    </leafNode>
    <leafNode name="optimize-network-throughput">
      <properties>
        <help>Provides optimal network usage while ensuring that the application performance is not compromised</help>
        <valueless/>
      </properties>
    </leafNode>
    <node name="prevent-message-fragmentation">
      <properties>
        <help>Detects the message, packet or data blocking characteristics of the serial data and preserves it through the communication</help>
      </properties>
      <children>
        <leafNode name="delay-between-messages">
          <properties>
            <help>Delay sending between messages</help>
            <valueHelp>
              <format>u32:0-65535</format>
              <description>Delay in milliseconds</description>
            </valueHelp>
            <constraint>
              <validator name="numeric" argument="--range 0-65535"/>
            </constraint>
          </properties>
          <defaultValue>250</defaultValue>
        </leafNode>
      </children>
    </node>
    <node name="custom">
      <properties>
        <help>Custom packet forwarding setting</help>
      </properties>
      <children>
        <leafNode name="forwarding-rule">
          <properties>
            <help>Forwarding rule</help>
            <completionHelp>
              <list>strip trigger trigger+1 trigger+2</list>
            </completionHelp>
            <valueHelp>
              <format>strip</format>
              <description>Strips out the EOF1, EOF1/EOF2, Trigger1, or Trigger1/Trigger2, depending on your settings</description>
            </valueHelp>
            <valueHelp>
              <format>trigger</format>
              <description>Includes the EOF1, EOF1/EOF2, Trigger1, or Trigger1/Trigger2, depending on your settings</description>
            </valueHelp>
            <valueHelp>
              <format>trigger+1</format>
              <description>Includes the EOF1, EOF1/EOF2, Trigger1, or Trigger1/Trigger2, depending on your settings, plus the first byte that follows the trigger</description>
            </valueHelp>
            <valueHelp>
              <format>trigger+2</format>
              <description>Trigger+2—Includes the EOF1, EOF1/EOF2, Trigger1, or Trigger1/Trigger2, depending on your settings, plus the next two bytes received after the trigger</description>
            </valueHelp>
            <constraint>
              <regex>(strip|trigger|trigger\+1|trigger\+2)</regex>
            </constraint>
          </properties>
          <defaultValue>trigger</defaultValue>
        </leafNode>
        <node name="packet">
          <properties>
            <help>Custom packet forwarding setting</help>
          </properties>
          <children>
            <leafNode name="packet-size">
              <properties>
                <help>Packet size</help>
                <valueHelp>
                  <format>u32:0-1024</format>
                  <description>Packet size in bytes</description>
                </valueHelp>
                <constraint>
                  <validator name="numeric" argument="--range 0-1024"/>
                </constraint>
              </properties>
            </leafNode>
            <leafNode name="idle-timer">
              <properties>
                <help>Idle timer</help>
                <valueHelp>
                  <format>u32:0-65535</format>
                  <description>Idle timer in milliseconds</description>
                </valueHelp>
                <constraint>
                  <validator name="numeric" argument="--range 0-65535"/>
                </constraint>
              </properties>
            </leafNode>
            <leafNode name="force-transmit-timer">
              <properties>
                <help>Force transmit timer</help>
                <valueHelp>
                  <format>u32:0-65535</format>
                  <description>Force transmit timer in milliseconds</description>
                </valueHelp>
                <constraint>
                  <validator name="numeric" argument="--range 0-65535"/>
                </constraint>
              </properties>
            </leafNode>
            <leafNode name="trigger-value1">
              <properties>
                <help>End trigger first hex value</help>
                <valueHelp>
                  <format>txt</format>
                  <description>End trigger first char</description>
                </valueHelp>
                <constraint>
                  <validator name="hex"/>
                </constraint>
              </properties>
            </leafNode>
            <leafNode name="trigger-value2">
              <properties>
                <help>End trigger second hex value</help>
                <valueHelp>
                  <format>txt</format>
                  <description>End trigger second char</description>
                </valueHelp>
                <constraint>
                  <validator name="hex"/>
                </constraint>
              </properties>
            </leafNode>
          </children>
        </node>
        <node name="frame">
          <properties>
            <help>Custom frame forwarding setting</help>
          </properties>
          <children>
            <leafNode name="start-value1">
              <properties>
                <help>Start of frame first hex value</help>
                <valueHelp>
                  <format>txt</format>
                  <description>Start of frame first char</description>
                </valueHelp>
                <constraint>
                  <validator name="hex"/>
                </constraint>
              </properties>
            </leafNode>
            <leafNode name="end-value1">
              <properties>
                <help>End of frame first hex value</help>
                <valueHelp>
                  <format>txt</format>
                  <description>End of frame first char</description>
                </valueHelp>
                <constraint>
                  <validator name="hex"/>
                </constraint>
              </properties>
            </leafNode>
            <leafNode name="start-value2">
              <properties>
                <help>Start of frame second hex value</help>
                <valueHelp>
                  <format>txt</format>
                  <description>Start of frame second char</description>
                </valueHelp>
                <constraint>
                  <validator name="hex"/>
                </constraint>
              </properties>
            </leafNode>
            <leafNode name="end-value2">
              <properties>
                <help>End of frame second hex value</help>
                <valueHelp>
                  <format>txt</format>
                  <description>End of frame second char</description>
                </valueHelp>
                <constraint>
                  <validator name="hex"/>
                </constraint>
              </properties>
            </leafNode>
            <leafNode name="transmit-start-characters">
              <properties>
                <help>Enable transmit start of frame character(s)</help>
                <valueless/>
              </properties>
            </leafNode>
          </children>
        </node>
      </children>
    </node>
  </children>
</node>
<!-- include end -->
